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
or None. A harness gives it (the adapter of hermes, from Hermes's own table).

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

from . import metrics, ports

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
)
KEYS = tuple(m["key"] for m in CACHE)
_RATIOS = tuple(m for m in CACHE if m["agg"] == "ratio")
_AGG = {m["key"]: m["agg"] for m in CACHE}


def rates(model, provider):
    """The rates of the price port for a model: (input, cache read, cache write) in USD per 1M tokens, or None."""
    price = ports.get("price")
    if price is None or not model:
        return None
    try:
        r = price(model, provider)
        r = None if r is None else tuple(float(x) for x in r[:3])  # a missing rate (None) raises here
        return r if r is not None and len(r) == 3 else None
    except Exception:  # a port that fails, or a rate that is missing: no price, never an error in a view
        return None


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
    (numerator, denominator); cache_saved_usd is there only when price(model, provider) gives rates."""
    records = list(records)
    broke, out, known = breaks(records), [], {}
    for i, r in enumerate(records):
        v = {m["key"]: metrics.part(m, r) for m in _RATIOS}
        v["cache_breaks"], v["break_tokens"] = (1, r["cw"]) if i in broke else (0, 0)
        at = (r.get("model"), r.get("prov"))
        if price and at not in known:  # one ask of the price port for each model and provider
            known[at] = price(*at)
        usd = saved(r, known.get(at))
        if usd is not None:
            v["cache_saved_usd"] = usd
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
