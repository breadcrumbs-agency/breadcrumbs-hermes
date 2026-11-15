"""The usage of a model request, and the cache metrics: defined once, here.

A usage record is one model request, a dict:

    sess, req, ts, task      the session id, the request id, seconds since 1970, "main" or the task of a helper call
    model, prov              the model and the provider that served it ("" or None when not known)
    in, cr, cw, out, rsn     tokens: input that no cache held, cache read, cache write, output, reasoning
    prompt                   in + cr + cw: the context that the provider counted

Each source turns its own format into these fields at the source. In particular, an OpenAI-style input count holds the
cached tokens: there in = input - cached - cache write.

The cache metrics (added to the table of breadcrumbs/metrics.py):

    cache_hit        ratio  cr / prompt
    cache_write_share ratio cw / prompt (the key cache_write is the viewer's count of cache write tokens)
    cache_reuse      ratio  cr / cw: the tokens read back for each token written
    cache_breaks     sum    the requests whose cr is below the cr of the request before them in the same session (and
                            task), with cw > 0: the cached prefix was lost and written again. Order: time, then id.
    break_tokens     sum    the cw of those requests
    cache_saved_usd  sum    cr * (rate_in - rate_cr) - cw * (rate_cw - rate_in) per request, at the rates of the price
                            port (USD per 1M tokens). No port, an unknown model or a missing rate: no number.

The price port, ports.get("price"): price(model, provider) -> (input, cache read, cache write) in USD per 1M tokens,
or a list of (upto, rates) tiers for a price that steps up with the prompt (costs.pick), or None. A harness gives it
(the adapter of hermes: the cost profile of costs.py first, then Hermes's own table).

The cache writes beside the ACE acts (AFTER, group "ace"), for each act A of ACTS (pass, keep, fold, ratchet), counted on
the main requests that the port ace_join (the harness that writes ace.log) joins ace.log lines to. A request counts once,
for one act: of the lines joined to it, the act of highest rank (RANK: fold > ratchet > keep > pass):

    acts_<A>          sum    the requests whose act is A
    cw_after_<A>      sum    the cw of the request after an act A
    breaks_after_<A>  sum    those requests that are cache breaks
    cw_per_<A>        ratio  cw_after_<A> / acts_<A>
    break_rate_<A>    ratio  breaks_after_<A> / acts_<A>
    fold_cw_share     ratio  cw_after_fold / the cw of all main requests (in the same cells)

Where the records go: usage.log in the store home, one line for each request, the folder and the format of the lines that
the hooks of Hermes write (breadcrumbs_hermes/usage_log.py) and that the viewer reads. line() makes a line; a record that a
harness gave carries src=<harness> at its end. append() adds the records of an ingest, and skips each one whose request
id (req) is in usage.log or usage.log.1 already: a second ingest of the same files adds no line.
"""
import math
import os
import re
import time
from collections.abc import Mapping

from . import costs, metrics, ports

COUNTS = ("in", "cr", "cw", "out", "rsn")  # the token fields of a record
PROMPT = ("in", "cr", "cw")  # the parts of the prompt
_AT = ("time", "request")  # the context units in which a request has a position
CACHE = metrics.add(
    metrics.metric("cache_hit", "cache hit (share of the prompt)", "share", "ratio", "usage", "usage.log cr / (in + cr + cw)",
                   _AT, keyed=True, of=("cr", PROMPT)),
    metrics.metric("cache_write_share", "cache write (share of the prompt)", "share", "ratio", "usage",
                   "usage.log cw / (in + cr + cw)", _AT, keyed=True, of=("cw", PROMPT)),
    metrics.metric("cache_reuse", "cache reuse (reads per token written)", "ratio", "ratio", "usage", "usage.log cr / cw", _AT,
                   keyed=True, of=("cr", ("cw",))),
    metrics.metric("cache_breaks", "cache breaks", "count", "sum", "usage",
                   "usage.log: cr below the request before it in the session, with cw > 0", _AT, keyed=True),
    metrics.metric("break_tokens", "tokens written again at a break", "tokens", "sum", "usage", "usage.log cw of a cache break",
                   _AT, keyed=True),
    metrics.metric("cache_saved_usd", "saved by the cache", "usd", "sum", "usage",
                   "usage.log cr, cw at the rates of the price port", _AT, keyed=True),
    metrics.metric("cache_store_usd", "cache store (writes)", "usd", "sum", "cache_cost",
                   "usage.log cw at the cache write rate (price port)", _AT, keyed=True),
    metrics.metric("cache_read_usd", "cache read", "usd", "sum", "cache_cost",
                   "usage.log cr at the cache read rate (price port)", _AT, keyed=True),
    metrics.metric("cache_uncached_usd", "same tokens, uncached", "usd", "sum", "cache_cost",
                   "usage.log cr + cw at the input rate: what they cost without the cache (price port)", _AT, keyed=True),
)
KEYS = tuple(m["key"] for m in CACHE)
COST_KEYS = ("cache_store_usd", "cache_read_usd", "cache_uncached_usd")  # cost metrics: need the rates of the price port
_RATIOS = tuple(m for m in CACHE if m["agg"] == "ratio")
_AGG = {m["key"]: m["agg"] for m in CACHE}


def rates(model, provider):
    """The price of a model from the price port: (input, cache read, cache write) in USD per 1M tokens; or a list of
    (upto, rates) tiers for a price that steps up with the size of the prompt (costs.pick picks one); or None."""
    price = ports.get("price")
    if price is None or not model:
        return None
    try:
        r = price(model, provider)
        if isinstance(r, list):  # a missing rate (None) raises here
            tiers = [(upto, tuple(float(x) for x in rr[:3])) for upto, rr in r]
            return tiers if tiers and all(len(rr) == 3 for _, rr in tiers) else None
        r = None if r is None else tuple(float(x) for x in r[:3])  # a missing rate (None) raises here
        return r if r is not None and len(r) == 3 else None
    except Exception:  # a port that fails, or a rate that is missing: no price, never an error in a view
        return None


LEASE_TTL = 300        # seconds that a cache write lasts after its last read: the 5 minute tier
LEASE_TTL_LONG = 3600  # the 1 hour tier
# A line of usage.log does not say which tier its request asked for. leases() reads it off the requests of the session:
# when a request read ALL that its cache held more than LEASE_TTL after the read before it, the session is on the long
# tier; with no such read it is on the short one. Only a read of the top counts: the lowest part of a cache (the system
# prompt) can be the same bytes in another chat, which keeps it alive.


def _began(r):
    """When a request began. The stamp of a record (ts) is the end of its request, so it began dur seconds before
    (dur: the seconds that it took; a record without it began at ts). The provider reads and writes the cache then."""
    try:
        return r["ts"] - max(0.0, float(r.get("dur") or 0.0))
    except (TypeError, ValueError):
        return r["ts"]


def _money(r, price, priced):
    """The dollars of one main request at its rates (costs.pick by its prompt): store (cw at the cache write rate),
    read (cr at the cache read rate), unc_read and unc_write (cr and cw at the input rate: what they cost uncached),
    and tier (the place of those rates in a price that steps up with the prompt: 0 for the first tier and for a flat
    price). None when the price port gives no rates for it."""
    at = (r.get("model"), r.get("prov"))
    if at not in priced:
        priced[at] = price(*at) if price else None
    prompt = sum(r[k] for k in PROMPT)
    pr = costs.pick(priced[at], prompt)
    if pr is None:
        return None
    rin, rcr, rcw = pr
    tiers = priced[at] if isinstance(priced[at], list) else ()
    tier = next((i for i, (upto, _) in enumerate(tiers) if upto is None or prompt <= upto), max(0, len(tiers) - 1))
    return {"store": r["cw"] * rcw / 1e6, "read": r["cr"] * rcr / 1e6,
            "unc_read": r["cr"] * rin / 1e6, "unc_write": r["cw"] * rin / 1e6, "tier": tier}


def _lease(sess, r, t, lo, hi, up, run, m=None, unseen=False):
    """A lease while _grow builds the tree: the tokens lo to hi, written at t, on the lease `up`, in the run `run`."""
    return {"sess": sess, "model": r.get("model"), "prov": r.get("prov"), "req": None if unseen else r.get("req"),
            "start": t, "lo": lo, "hi": hi, "up": up,
            "run": run, "over": [], "own": 0, "rcr": 0.0, "rin": 0.0, "tier": m["tier"] if m else 0,
            "store_usd": m["store"] if m else 0.0, "unc_write": m["unc_write"] if m else 0.0, "unseen": unseen}


def _where(items, n):
    """The place of the lease n in a list (the lease itself, not one that is equal to it)."""
    return next(i for i, x in enumerate(items) if x is n)


def _cut(out, tops, n, at):
    """A request read the first `at` tokens of the cache, and the lease n holds that size inside it (lo < at < hi): n is
    cut in two there. The lower part (lo to at) is a new lease under n, and n keeps at to hi. Both are of the one write,
    so its money is shared by their tokens. -> the lower part."""
    share = (at - n["lo"]) / (n["hi"] - n["lo"])
    low = dict(n, hi=at, over=[n], own=0, rcr=0.0, rin=0.0,
               store_usd=n["store_usd"] * share, unc_write=n["unc_write"] * share)
    if n["up"] is not None:
        n["up"]["over"][_where(n["up"]["over"], n)] = low
    n.update(lo=at, up=low, store_usd=n["store_usd"] - low["store_usd"], unc_write=n["unc_write"] - low["unc_write"])
    n["run"]["items"].insert(_where(n["run"]["items"], n), low)
    out.insert(_where(out, n), low)
    tops.setdefault(at, []).append(low)
    return low


def _grow(sess, rs, ttl, price, priced):
    """The leases that the main requests of one session on one model (rs, in the order that they began) wrote and read,
    when a lease lasts ttl seconds after its last read. The cache is a tree of prefixes: a request reads the lease that
    ends at its cr (and with it every lease under that one), then writes its cw on top of it as a new lease. A run is a
    line of leases that stand on each other with no branch: one read reads them all, so a run has one time of the last
    read ({"last", "items": its leases, the lowest first}), and a read costs a step for each branch under it, not for
    each lease.
    -> (leases, marks, orphan, widest): a lease comes after the lease that it stands on; widest is the longest pause
    that a read of all the cache bridged."""
    out, marks, tops, pool, widest = [], [], {}, [], 0.0
    orphan = {"reads": 0, "read_tok": 0, "read_usd": 0.0, "uncached_usd": 0.0}
    alive = lambda n, t: n["run"]["last"] + ttl >= t
    newest = lambda n: (n["run"]["last"], n["start"])
    for r in rs:
        t, cr, cw = _began(r), r["cr"], r["cw"]
        m = _money(r, price, priced)
        hit = under = new = None  # the lease that it read, the lease that its write stands on, the lease that it wrote
        kind = "miss" if cw > 0 else "none"
        if cr > 0:
            kind = "hit"
            live = tops.get(cr)
            if live:
                live[:] = [n for n in live if alive(n, t)]  # a lease that the ttl ended is never read again
            if live:
                hit = max(live, key=newest)
            else:
                pool[:] = [n for n in pool if alive(n, t)]
                live = [n for n in pool if n["lo"] < cr < n["hi"]]
                if live:
                    hit = _cut(out, tops, max(live, key=newest), cr)
                    pool.append(hit)
            if hit is None:  # the log holds no write of what it read (or the ttl had ended it): an orphan read
                orphan["reads"] += 1
                orphan["read_tok"] += cr
                if m:
                    orphan["read_usd"] += m["read"]
                    orphan["uncached_usd"] += m["unc_read"]
                under = _lease(sess, r, t, 0, cr, None, {"last": t, "items": []}, unseen=True)  # kept from here on
                under["run"]["items"].append(under)
                out.append(under)
                pool.append(under)
                tops.setdefault(cr, []).append(under)
            else:
                under, run = hit, hit["run"]
                if any(alive(n, t) for n in reversed(hit["over"])):
                    kind = "part"  # the cache held more on top of what this request read
                else:
                    widest = max(widest, t - run["last"])
                if run["items"][-1] is not hit:  # the leases over it in its run are not read with it any more
                    i = _where(run["items"], hit)
                    above = {"last": run["last"], "items": run["items"][i + 1:]}
                    del run["items"][i + 1:]
                    for n in above["items"]:
                        n["run"] = above
                while run is not None and run["last"] < t:  # a read of a lease is a read of every lease under it
                    run["last"] = t
                    low = run["items"][0]["up"]
                    run = low["run"] if low is not None else None
                hit["own"] += 1
                if m:
                    hit["rcr"] += m["read"] / cr
                    hit["rin"] += m["unc_read"] / cr
        if cw > 0:
            if under is not None and not under["over"] and under["run"]["items"][-1] is under:
                run = under["run"]  # on top of its run: one read reads both from now on
            else:
                run = {"last": t, "items": []}
            new = _lease(sess, r, t, cr, cr + cw, under, run, m)
            run["items"].append(new)
            run["last"] = max(run["last"], t)
            if under is not None:
                under["over"].append(new)
            out.append(new)
            pool.append(new)
            tops.setdefault(cr + cw, []).append(new)
        marks.append({"sess": sess, "req": r.get("req"), "ts": r["ts"], "at": t, "cr": cr, "cw": cw, "inp": r["in"],
                      "kind": kind, "read": hit, "wrote": new, "tier": m["tier"] if m else 0,
                      "store_usd": m["store"] if m else None, "read_usd": m["read"] if m else None})
    for n in out:
        n["reads"], n["_rcr"], n["_rin"] = n["own"], n["rcr"], n["rin"]
    for n in reversed(out):  # the leases on top first: what read them read the lease under them too
        up = n["up"]
        if up is not None:
            up["reads"] += n["reads"]
            up["_rcr"] += n["_rcr"]
            up["_rin"] += n["_rin"]
    return out, marks, orphan, widest


def leases(records, ttl=None, price=rates, now=None):
    """The cache of the main requests as leases: rectangles of tokens in time. Also what each request did to them, and
    the reads that had no lease.

    A request reads the first cr tokens of its prompt from the cache (its hit) and writes the next cw tokens to it. The
    write is a lease: the tokens lo to hi (lo is the cr of its request), kept from the time that the request began
    (start) to ttl seconds after its last read (end). A later request that reads cr tokens reads the lease that ends at
    cr and every lease under it, so the leases of a chat stand on each other: `up` is the lease that one stands on, and
    the size of a hit is the height of the leases that it read. A request that reads less than the cache holds (the
    context over that point changed, as after a fold; or a second agent sent a context of its own under the same
    session id) writes its lease beside the ones that it did not read: those are still kept until their own end. A
    lease whose end is after now is "open", any other is "expired".

    ttl: the seconds that a lease lasts after its last read. None: the tier of each session is read off its requests
    (LEASE_TTL, or LEASE_TTL_LONG when a read of all the cache came more than LEASE_TTL after the read before it).

    A lease: {sess, model, prov, req (the id of the request that wrote it; None when unseen), id, up (the id of the
    lease under it, or None), lo, hi, start, last (when it was last
    read; its start when never), end, secs, why, ttl, write_tok, reads (the requests that read it), read_tok, tier (the
    price tier of its write), store_usd (its tokens at the cache write rate), read_usd (its tokens at the cache read
    rate, each time that they were read), uncached_usd (the same tokens at the input rate, for the write and for each
    read), unseen (True for cache that a request read with no write of it in the log: it starts at that read and has no
    write of its own)}.
    A mark, one for each request: {sess, req, ts, at (when it began), cr, cw, inp, kind, read (the id of the lease that
    ends at its cr, or None), wrote (the id of its lease, or None), tier, store_usd, read_usd}. kind: "hit" (it read all
    that its part of the cache held), "part" (it read only the lower part: more stood on top), "miss" (it read nothing
    and wrote), "none" (no cache token).
    orphan: {reads, read_tok, read_usd, uncached_usd} of the reads that had no lease. A request with no rates adds no
    money. Returns (leases, marks, orphan): the leases of a session in the order that they were written, the marks in
    the order that the requests began. Pure: nothing is written and no clock is read."""
    by = {}
    for r in records:
        if (r.get("task") or "main") == "main":
            by.setdefault((r["sess"], r.get("model")), []).append(r)  # a model has a cache of its own
    priced, found, marks = {}, [], []
    orphan = {"reads": 0, "read_tok": 0, "read_usd": 0.0, "uncached_usd": 0.0}
    for (sess, _), rs in by.items():
        rs.sort(key=_began)
        use = ttl
        if use is None:
            use = LEASE_TTL_LONG if _grow(sess, rs, LEASE_TTL_LONG, None, {})[3] > LEASE_TTL else LEASE_TTL
        grown, ms, orph, _ = _grow(sess, rs, use, price, priced)
        for i, n in enumerate(grown):
            n["id"] = len(found) + i
        for n in grown:
            size, last = n["hi"] - n["lo"], n["run"]["last"]
            end = last + use
            found.append({"sess": sess, "model": n["model"], "prov": n["prov"], "req": n["req"], "id": n["id"],
                          "up": n["up"]["id"] if n["up"] is not None else None, "lo": n["lo"], "hi": n["hi"],
                          "start": n["start"], "last": last, "end": end, "secs": end - n["start"],
                          "why": "open" if now is not None and end > now else "expired", "ttl": use,
                          "write_tok": 0 if n["unseen"] else size, "reads": n["reads"], "read_tok": n["reads"] * size,
                          "tier": n["tier"], "store_usd": n["store_usd"], "read_usd": size * n["_rcr"],
                          "uncached_usd": n["unc_write"] + size * n["_rin"], "unseen": n["unseen"]})
        for mk in ms:
            mk["read"] = mk["read"]["id"] if mk["read"] is not None else None
            mk["wrote"] = mk["wrote"]["id"] if mk["wrote"] is not None else None
        marks += ms
        for k in orphan:
            orphan[k] += orph[k]
    marks.sort(key=lambda mk: mk["at"])
    return found, marks, orphan


def lease_totals(found, marks, orphan):
    """The sums of leases(): n (leases), open (those still kept), requests, reads (the requests that read a lease), kinds
    (the requests by the kind of their mark), write_tok, read_tok, store_usd, read_usd, cache_usd (store + read), uncached_usd, saved_usd
    (uncached - cache), ratio (cache / uncached: below 1 the cache cost less), unread ({n, tok, usd}: the leases that
    ended with no read, so their write bought nothing), and the orphan reads. The orphan reads are not in cache_usd, so
    they are not in ratio either."""
    t = {"n": len(found), "open": sum(1 for ls in found if ls["why"] == "open"), "requests": len(marks),
         "reads": sum(1 for mk in marks if mk["read"] is not None), "kinds": {},
         "write_tok": 0, "read_tok": 0, "store_usd": 0.0, "read_usd": 0.0, "uncached_usd": 0.0}
    unread = {"n": 0, "tok": 0, "usd": 0.0}
    for mk in marks:
        t["kinds"][mk["kind"]] = t["kinds"].get(mk["kind"], 0) + 1
    for ls in found:
        t["write_tok"] += ls["write_tok"]
        t["read_tok"] += ls["read_tok"]
        t["store_usd"] += ls["store_usd"]
        t["read_usd"] += ls["read_usd"]
        t["uncached_usd"] += ls["uncached_usd"]
        if not ls["reads"] and ls["write_tok"] and ls["why"] == "expired":
            unread["n"] += 1
            unread["tok"] += ls["write_tok"]
            unread["usd"] += ls["store_usd"]
    t["cache_usd"] = t["store_usd"] + t["read_usd"]
    t["saved_usd"] = t["uncached_usd"] - t["cache_usd"]
    t["ratio"] = t["cache_usd"] / t["uncached_usd"] if t["uncached_usd"] > 0 else None
    t["unread"] = unread
    t["orphan"] = dict(orphan)
    return t


def saved(record, r):
    """The dollars that the cache saved on one request at the rates r (in, cr, cw per 1M), or None without rates."""
    if r is None:
        return None
    rin, rcr, rcw = r
    return (record["cr"] * (rin - rcr) - record["cw"] * (rcw - rin)) / 1e6


def breaks(records):
    """The positions (in `records`) of the requests that are cache breaks: in each session and task, ordered by time and
    then by request id, a request whose cr is below the cr of the one before it, with cw > 0."""
    seqs = {}
    for i, r in enumerate(records):
        seqs.setdefault((r["sess"], r.get("task") or "main"), []).append(i)
    out = set()
    for seq in seqs.values():
        seq.sort(key=lambda i: (records[i]["ts"], records[i].get("req") or ""))
        out.update(b for a, b in zip(seq, seq[1:]) if records[b]["cr"] < records[a]["cr"] and records[b]["cw"] > 0)
    return out


def values(records, price=rates):
    """What each record gives to the cache metrics, in the order given: [{metric key: value}]. A ratio gives its pair
    (numerator, denominator); cache_saved_usd is there only when price(model, provider) gives rates. A price with
    tiers is picked by the prompt of each record (costs.pick)."""
    records = list(records)
    broke, out, known = breaks(records), [], {}
    for i, r in enumerate(records):
        v = {m["key"]: metrics.part(m, r) for m in _RATIOS}
        v["cache_breaks"], v["break_tokens"] = (1, r["cw"]) if i in broke else (0, 0)
        at = (r.get("model"), r.get("prov"))
        if price and at not in known:  # one ask of the price port for each model and provider
            known[at] = price(*at)
        usd = saved(r, costs.pick(known.get(at), sum(r[k] for k in PROMPT)))
        if usd is not None:
            v["cache_saved_usd"] = usd
        money = _money(r, price, known) if price else None  # the dollars of the cache, the same tokens uncached
        if money is not None:
            v["cache_store_usd"], v["cache_read_usd"] = money["store"], money["read"]
            v["cache_uncached_usd"] = money["unc_read"] + money["unc_write"]
        out.append(v)
    return out


def _numbers(pairs):
    acc, n = {}, 0
    for r, v in pairs:
        n += 1
        for k in COUNTS:
            acc[k] = acc.get(k, 0) + r[k]
        for k, x in v.items():
            acc[k] = metrics.fold(_AGG[k], acc[k], x) if k in acc else x
    out = {"requests": n}
    out.update((k, acc.get(k, 0)) for k in COUNTS)
    for k in KEYS:
        x = metrics.value(_AGG[k], acc.get(k))
        if x is not None:
            out[k] = round(x, 6 if k == "cache_saved_usd" else 4)
    return out


def summary(records, price=rates):
    """The token totals and the cache metrics of the records, in all and by model: {requests, in, cr, cw, out, rsn,
    <cache metric>..., models: {model: the same}}. A metric with no number (a 0 denominator, no rates) is left out."""
    records = list(records)
    pairs = list(zip(records, values(records, price)))
    out = _numbers(pairs)
    by = {}
    for r, v in pairs:
        by.setdefault(r.get("model") or "-", []).append((r, v))
    out["models"] = {model: _numbers(by[model]) for model in sorted(by)}
    return out


# -- the cache writes beside the ACE acts --------------------------------------------------------------------------------
# An ACE act (an ace.log line: pass, keep, fold, ratchet) shapes the next main request of its session, and a fold rewrites
# the context, so that request writes the cache again. The harness that writes ace.log joins each line to that request (the
# port ace_join); these metrics count what the joined requests wrote. A line that no request follows gives nothing. Two or
# more lines can join one request (ace.log and usage.log stamp whole seconds); the request then counts once, for the act of
# highest rank of its lines (RANK), so no cache write and no break counts twice.
ACTS = ("pass", "keep", "fold", "ratchet")
RANK = ("fold", "ratchet", "keep", "pass")  # of the lines joined to one request, the first act here is the request's act
_PER_ACT = ("acts_", "cw_after_", "breaks_after_", "cw_per_", "break_rate_")  # the metrics of each act: <prefix><act>
_JOINED = "ace.log act=%s, joined to the next main request (usage.log), the act of highest rank of its lines"
AFTER = metrics.add(*[m for act in ACTS for m in (
    metrics.metric("acts_" + act, "requests after a %s" % act, "count", "sum", "ace", _JOINED % act, _AT, keyed=True),
    metrics.metric("cw_after_" + act, "cache write after a %s" % act, "tokens", "sum", "ace",
                   "usage.log cw of the request after an " + _JOINED % act, _AT, keyed=True),
    metrics.metric("breaks_after_" + act, "cache breaks after a %s" % act, "count", "sum", "ace",
                   "cache_breaks of the request after an " + _JOINED % act, _AT, keyed=True),
    metrics.metric("cw_per_" + act, "cache write per %s" % act, "tokens", "ratio", "ace",
                   "cw_after_%s / acts_%s" % (act, act), _AT, keyed=True, of=("cw_after_" + act, ("acts_" + act,))),
    metrics.metric("break_rate_" + act, "cache breaks per %s" % act, "share", "ratio", "ace",
                   "breaks_after_%s / acts_%s" % (act, act), _AT, keyed=True, of=("breaks_after_" + act, ("acts_" + act,))))],
    metrics.metric("fold_cw_share", "cache write after a fold (share of all)", "share", "ratio", "ace",
                   "cw_after_fold / usage.log cw of the main requests", _AT, keyed=True, of=("cw_after_fold", ("cw",))))
AFTER_KEYS = tuple(m["key"] for m in AFTER)
_AFTER = {m["key"]: m for m in AFTER}


def rank(act):
    """The place of an act in RANK (0 is the highest). An act that is not in RANK (intercept) comes after all of them."""
    return RANK.index(act) if act in RANK else len(RANK)


def after(records, joins):
    """What the requests and the ACE acts give to the metrics of AFTER. records: usage records (the requests of one
    session, every task); joins: [(act, i)]: an ace.log line of that act, joined to the main request records[i].
    -> {i: {metric key: value}}, a ratio as its pair: each main request gives its cw to the denominator of fold_cw_share;
    each joined request gives, for its act (of its lines, the act of highest rank in RANK; a line of another act, as
    intercept, gives nothing), 1 to acts_<act>, its cw to cw_after_<act>, 1 to breaks_after_<act> when it is a cache break
    (breaks()), and the parts of the ratios of its act. A request counts once, however many lines join it."""
    broke, out, won = breaks(records), {}, {}

    def give(i, rec, keys):
        vals = out.setdefault(i, {})
        for k in keys:
            m = _AFTER[k]
            v = metrics.part(m, rec) if m["agg"] == "ratio" else rec[k]
            vals[k] = metrics.fold(m["agg"], vals[k], v) if k in vals else v

    for i, r in enumerate(records):
        if (r.get("task") or "main") == "main":
            give(i, {"cw_after_fold": 0, "cw": r["cw"]}, ("fold_cw_share",))
    for act, i in joins:
        if act in RANK and (i not in won or rank(act) < rank(won[i])):  # an act not in RANK has no metrics
            won[i] = act
    for i, act in won.items():
        rec = {"acts_" + act: 1, "cw_after_" + act: records[i]["cw"], "breaks_after_" + act: int(i in broke), "cw": 0}
        give(i, rec, [k + act for k in _PER_ACT] + (["fold_cw_share"] if act == "fold" else []))
    return out


# -- the record and its line ---------------------------------------------------------------------------------------------
LOG_NAME = "usage.log"
LOGS = (LOG_NAME + ".1", LOG_NAME)  # the older file first, as the viewer reads them
WORD_MAX = 256  # an id or a name is cut here: a line holds ids and numbers, never text
LINE = (("sess", "word"), ("turn", "word"), ("req", "word"), ("n", "count"), ("task", "word"), ("model", "word"),
        ("resp", "word"), ("prov", "word"), ("in", "count"), ("cr", "count"), ("cw", "count"), ("out", "count"),
        ("rsn", "count"), ("prompt", "count"), ("est", "count"), ("msgs", "count"), ("tools", "count"), ("dur", "seconds"),
        ("fin", "word"))  # the fields of a line, in order; src (the harness that gave the record) follows when it is known
_SPACE = re.compile(r"\s+")
_REQ = re.compile(r" req=(\S+)")


def word(value):
    """An id or a name as one word: whitespace becomes "_" and a missing value is "-". A structure is never an id."""
    if value is None or isinstance(value, (Mapping, list, tuple, set, frozenset, bytes, bytearray)):
        return "-"
    text = "".join(c if c.isprintable() else "_" for c in _SPACE.sub("_", str(value).strip()))
    return text[:WORD_MAX] or "-"


def count(value):
    """A count as a whole number: 0 when it is missing, not a number or below 0."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def seconds(value):
    """Seconds with 3 decimals: 0.000 when they are missing, not a number or below 0."""
    if value is None or isinstance(value, bool):
        return "0.000"
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return "0.000"
    return "%.3f" % x if math.isfinite(x) and x > 0 else "0.000"


_AS = {"word": word, "count": count, "seconds": seconds}


def line(fields, stamp=None):
    """The line (no newline) of one request: "<local time> key=value ...", the fields of LINE in order (a missing one is
    "-" or 0), then src=<harness> when fields has a src. stamp: the time as text; None: the local time of fields["ts"]."""
    if stamp is None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(fields["ts"]))
    out = [stamp] + ["%s=%s" % (k, _AS[how](fields.get(k))) for k, how in LINE]
    if fields.get("src"):
        out.append("src=" + word(fields["src"]))
    return " ".join(out)


def record(src, sess, req, ts, model, prov, inp, cr, cw, out, rsn, task="main"):
    """One usage record, as a source gives it: the counts of the provider, already in the meaning of this module (inp:
    the input that no cache held)."""
    inp, cr, cw = count(inp), count(cr), count(cw)
    return {"src": src, "sess": sess, "req": req, "ts": ts, "task": task, "model": model, "resp": model, "prov": prov,
            "in": inp, "cr": cr, "cw": cw, "out": count(out), "rsn": count(rsn), "prompt": inp + cr + cw}


def known(home):
    """The request ids of the lines of usage.log and usage.log.1 in home ("-", no id, is left out)."""
    ids = set()
    for name in LOGS:
        try:
            with open(os.path.join(home, name), encoding="utf-8", errors="replace") as f:
                ids.update(m.group(1) for m in map(_REQ.search, f) if m)
        except OSError:
            continue
    ids.discard("-")
    return ids


def append(home, records):
    """Add a line to <home>/usage.log for each record whose request id is not there yet (nor in usage.log.1, nor before
    it in records). One write of all the lines; the file is not turned over here (the hooks of Hermes do that at their
    next line). Returns the count of lines added."""
    have, lines = known(home), []
    for r in records:
        req = word(r.get("req"))
        if req == "-" or req in have:
            continue
        have.add(req)
        lines.append(line(r) + "\n")
    if lines:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, LOG_NAME), "ab", buffering=0) as f:
            f.write("".join(lines).encode("utf-8"))
    return len(lines)
