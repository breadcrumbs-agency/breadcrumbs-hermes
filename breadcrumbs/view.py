"""Views of a store: the explorer's data, the trail, and a read-only server on 127.0.0.1.

The page (view.html) lays crumbs out as SpaceGraph does: Graph2D nodes sized by priority, the
Force2D layout, and the Zoomed camera, whose zoom stack is the trail. This module gives
the page its data. It never writes to the store or the index. The API needs the token in
<root>/view.token, so another web page cannot read the memory through the loopback port.
"""

import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import dials
from .crumbs import Crumb, is_crumb
from .history import Refs
from .index import Index
from .store import Store
from .ingest import leaves
from .zoom import CUED, GAP, Zoom

STACK_MAX = 8  # SpaceGraph's Zoomed.ZOOM_STACK_MAX
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "view.html")
HEAT_HALF = 86400.0  # seconds: the glow of a crumb the model opened halves each day
ACE_LOG = ("ace.log.1", "ace.log")  # oldest first
ACE_KV = re.compile(r"(\w+)=(\S+)")
CUT_EDGES = (0.05, 0.10, 0.20, 0.40)  # a fold cuts less than 5%, 5 to 10%, 10 to 20%, 20 to 40%, 40% or more
CANDLES = 40  # about this many candles in a chart


class Trail:
    """The zoom stack of one session, as in SpaceGraph's Zoomed: the top crumb again changes only its
    level; a crumb lower in the stack goes back to it; a new crumb is pushed, and past STACK_MAX the
    oldest leaves (the status line shows one gap mark for them)."""

    def __init__(self, stack=(), dropped=0, session=""):
        self.stack, self.dropped, self.session = [dict(e) for e in stack], dropped, session

    def visit(self, ref, level, label=""):
        at = [e["id"] for e in self.stack]
        if ref in at:
            del self.stack[at.index(ref) + 1:]
            self.stack[-1]["level"] = level
            if label:
                self.stack[-1]["label"] = label
            return
        self.stack.append({"id": ref, "level": level, "label": label or ref})
        if len(self.stack) > STACK_MAX:
            del self.stack[0]
            self.dropped += 1

    def line(self):
        return " \u203a ".join((["\u2026"] if self.dropped else []) + [e["label"] for e in self.stack])

    def to_dict(self):
        return {"session": self.session, "stack": self.stack, "dropped": self.dropped, "line": self.line()}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("stack", ()), d.get("dropped", 0), d.get("session", ""))


def label(zoom, ref):
    """A short trail name: the kind, then three cue words (or the title)."""
    head = zoom.handle(ref).strip()
    if head.startswith(GAP):
        return "text " + ref.partition(":")[2]
    kind = (head.split(None, 3) + ["", "", "?"])[2]
    cues = head.partition(" | ")[2].split()[:3]
    title = re.search(r'"(.*?)"', head)
    return kind + " " + (" ".join(cues) if cues else (title.group(1)[:28] if title else ref))


def token(root):
    """The access token of the views (random, kept in <root>/view.token with mode 600)."""
    path = os.path.join(root, "view.token")
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def ace_requests(root):
    """Every request in <root>/ace.log.1 and ace.log, oldest first, as {sess, ts, act, raw, sent, count, base,
    crumbs, lay, folds, target}. raw is the request before ACE ("in"), sent the request after it ("out"), both
    in tokens by ACE's estimate; count is the model's own count of the request before (0: none yet). A line that
    does not parse, and a line with no line end, is skipped. Nothing is written."""
    out = []
    for name in ACE_LOG:
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")[:-1]
        except OSError:
            continue
        for line in lines:
            kv = dict(ACE_KV.findall(line[19:]))
            try:
                ts = time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
                row = {"sess": kv["sess"], "ts": ts, "act": kv["act"], "raw": int(kv["in"]), "sent": int(kv["out"]),
                       "count": int(kv.get("last", 0)), "base": None if kv.get("base", "None") == "None" else int(kv["base"]),
                       "crumbs": int(kv.get("layer", 0)), "lay": int(kv["lay"]) if "lay" in kv else None,
                       "folds": int(kv.get("folds", 0)), "target": int(kv["target"]) if "target" in kv else None}
            except (KeyError, ValueError, OverflowError):
                continue
            out.append(row)
    return out


def ace_series(rows):
    """The requests of one session, oldest first, with what a chart needs. drop is the share of the request before
    that this request cut (0 when it grew); seen is the model's own count of what this request sent (the next
    request carries it as last; None until then)."""
    pts = []
    for i, r in enumerate(rows):
        prev = rows[i - 1]["sent"] if i else 0
        nxt = rows[i + 1]["count"] if i + 1 < len(rows) else 0
        pts.append({**{k: r[k] for k in ("ts", "act", "raw", "sent", "base", "crumbs", "lay", "folds", "target")},
                    "i": i, "seen": nxt or None,
                    "drop": round(max(0.0, 1 - r["sent"] / prev), 4) if prev else 0.0})
    return pts


def ace_candles(pts, per=0):
    """Open, high, low and close of the sent size over runs of `per` requests (default: about CANDLES candles in
    all), with the folds inside each run."""
    per = per if per > 0 else max(1, -(-len(pts) // CANDLES))
    out = []
    for a in range(0, len(pts), per):
        run = pts[a:a + per]
        sent = [p["sent"] for p in run]
        out.append({"i0": run[0]["i"], "i1": run[-1]["i"], "ts0": run[0]["ts"], "ts1": run[-1]["ts"], "n": len(run),
                    "open": sent[0], "high": max(sent), "low": min(sent), "close": sent[-1],
                    "folds": sum(1 for p in run if p["act"] in ("fold", "ratchet"))})
    return out


def ace_mix(pts):
    """What the last request held, in tokens: base (the fixed part: system prompt and tools), layer (the crumbs),
    exact (the turns word for word) and held (the rest of the chat, one crumb call away). None for an empty
    session. A part that the log does not know (an older line has no base or no lay) counts 0 and is named in
    unknown."""
    if not pts:
        return None
    p = pts[-1]
    base, lay = p["base"] or 0, p["lay"] or 0
    unknown = [k for k in ("base", "lay") if p[k] is None]
    return {"i": p["i"], "base": base, "layer": lay, "exact": max(0, p["sent"] - base - lay), "sent": p["sent"],
            "raw": p["raw"], "held": max(0, p["raw"] - p["sent"]), "unknown": unknown}


def ace_totals(by):
    """The aggregate of all sessions in the log: the requests by act, the folds' cut sizes in bins (CUT_EDGES), and
    one summary row per session, the newest first. The first request of a session is no cut."""
    acts, cuts, rows = {}, [], []
    for sess, pts in by.items():
        mine = {}
        for p in pts:
            mine[p["act"]] = mine.get(p["act"], 0) + 1
            acts[p["act"]] = acts.get(p["act"], 0) + 1
            if p["act"] == "fold" and p["i"]:
                cuts.append(p["drop"])
        rows.append({"sess": sess, "n": len(pts), "t0": pts[0]["ts"], "t1": pts[-1]["ts"], "acts": mine,
                     "folds": mine.get("fold", 0) + mine.get("ratchet", 0), "max_sent": max(p["sent"] for p in pts),
                     "target": next((p["target"] for p in reversed(pts) if p["target"]), None)})
    rows.sort(key=lambda r: -r["t1"])
    bins = [0] * (len(CUT_EDGES) + 1)
    for c in cuts:
        bins[sum(1 for e in CUT_EDGES if c >= e)] += 1
    ordered = sorted(cuts)
    keeps = acts.get("keep", 0)
    return {"requests": sum(acts.values()), "sessions": len(rows), "acts": acts, "folds": len(cuts),
            "edges": list(CUT_EDGES), "bins": bins, "median_cut": ordered[len(ordered) // 2] if ordered else None,
            "cache_kept": round(keeps / (keeps + acts.get("fold", 0)), 4) if keeps + acts.get("fold", 0) else None,
            "rows": rows}


class View:
    """Read-only access to one breadcrumbs folder (the provider's root)."""

    def __init__(self, root, clock=time.time):
        """Open a store to read. A View creates and changes no file: a folder without a store raises
        FileNotFoundError. close() releases the files."""
        if not os.path.isfile(os.path.join(root, "index.db")):
            raise FileNotFoundError(os.path.join(root, "index.db"))
        self.root, self.clock, self._closed = root, clock, False
        self.store = Store(os.path.join(root, "store"), readonly=True)
        try:
            self.index = Index(os.path.join(root, "index.db"), readonly=True)
            self.refs = Refs(os.path.join(root, "refs"), readonly=True)
        except BaseException:
            self.store.close()
            raise
        self.zoom = Zoom(self.store, summaries=self.index.summary_of, cues=self.index.signature,
                         words=self.index.tiers)
        self.lock = threading.Lock()

    def _info(self, ref):
        blob_id = ref.partition(":")[0]
        blob = self.store.get(blob_id)
        info = {"id": ref, "kind": "text", "title": "", "cues": [], "session": "", "ts": 0,
                "tokens": self.zoom.full(ref)}
        if is_crumb(blob) and ":" not in ref:
            c = Crumb.decode(blob)
            info.update(kind=c.kind, title=c.title, session=c.meta.get("sess", ""), ts=c.meta.get("ts", 0),
                        cues=self.zoom.cue_words(blob_id).split() if c.kind in CUED else [])
        return info

    def _weigh(self, nodes):
        """NodeVis.pri in (0, 1] from the size (log scale); heat in [0, 1] from the last use."""
        top = max([n["tokens"] for n in nodes] + [1])
        used, now = self.index.heat([n["id"] for n in nodes]), self.clock()
        for n in nodes:
            n["pri"] = round((1 + math.log1p(n["tokens"])) / (1 + math.log1p(top)), 4)
            last = used.get(n["id"], (None,))[0]
            n["heat"] = round(0.5 ** (max(0.0, now - last) / HEAT_HALF), 4) if last else 0.0

    def graph(self, root=None, limit=300):
        """Nodes and edges. No root: the sessions that moved last, joined where they share cue words.
        A root: the crumb and its parts, one level (its summary first, if it has one)."""
        with self.lock:
            if root is None:
                names = [n for n in self.refs.names() if n.startswith("sess-")]
                moved = {n: os.stat(os.path.join(self.refs.path, n)).st_mtime_ns for n in names}
                names.sort(key=lambda n: (moved[n], n), reverse=True)
                refs = [self.refs.get(n) for n in names[:limit]]
                nodes = [self._info(r) for r in refs if r]
                edges = cue_edges(nodes)
            else:
                blob = self.store.get(root.partition(":")[0])
                parts = [] if ":" in root or not is_crumb(blob) else \
                    self.zoom.parts(Crumb.decode(blob), root)[:limit]
                nodes = [self._info(root)] + [self._info(p) for p in parts]
                edges = [{"source": root, "target": p, "kind": "part", "w": 1.0} for p in parts]
                for e, n in zip(edges, nodes[1:]):
                    if n["kind"] == "sum":
                        e["kind"] = "sum"
            self._weigh(nodes)
            return {"root": root, "nodes": nodes, "edges": edges}

    def crumb(self, ref, level):
        with self.lock:
            return {"id": ref, "level": level, "tokens": self.zoom.full(ref), "text": self.zoom.render(ref, level)}

    def find(self, q, k=8):
        """Search hits grouped by session: [{session, hits: [{id, ref, score}]}], best first."""
        from .hermes import _ref_name
        nb = dials.value(dials.config(self.root), None, "neighbor")
        with self.lock:
            groups, n = {}, 0
            for cid, ref, score in self.index.search(q, 3 * k, nb=nb):
                blob = self.store.get(cid)
                sid = Crumb.decode(blob).meta.get("sess", "") if is_crumb(blob) else ""
                sess = self.refs.get(_ref_name("sess-", sid)) if sid else None
                groups.setdefault(sess or cid, {"session": sess, "hits": []})["hits"].append(
                    {"id": cid, "ref": ref, "score": round(score, 3)})
                n += 1
                if n >= k:
                    break
            return list(groups.values())

    def path(self, ref, within=None, depth=4, budget=5000):
        """The crumbs from a start down to ref: [start, ..., ref], or [] when ref is not found.
        The start is `within`, else the session of ref (a text does not know its session)."""
        from .hermes import _ref_name
        with self.lock:
            blob = self.store.get(ref.partition(":")[0])
            start = within
            if start is None and is_crumb(blob) and ":" not in ref:
                sid = Crumb.decode(blob).meta.get("sess", "")
                start = self.refs.get(_ref_name("sess-", sid)) if sid else None
            if not start:
                return []
            up: dict = {start: None}
            level, seen = [start], 0
            for _ in range(depth + 1):
                if ref in up:
                    out = [ref]
                    while up[out[-1]] is not None:
                        out.append(up[out[-1]])
                    return out[::-1]
                below = []
                for r in level:
                    b = self.store.get(r.partition(":")[0])
                    if ":" in r or not is_crumb(b):
                        continue
                    for p in self.zoom.parts(Crumb.decode(b), r):
                        if p not in up and seen < budget:
                            up[p], seen = r, seen + 1
                            below.append(p)
                level = below
            return []

    def _crumb(self, ref):
        blob = self.store.get(ref.partition(":")[0])
        return Crumb.decode(blob) if ":" not in ref and is_crumb(blob) else None

    def _row(self, ref):
        c = self._crumb(ref)
        return {"id": ref, "kind": c.kind if c else "text", "label": label(self.zoom, ref), "session": self._sid(ref, c),
                "bars": []}

    def _sid(self, ref, c):
        """The session id of a crumb, or of the first indexed crumb that holds a text ("" when not known)."""
        if c is None:
            holder = self.index.holder(ref.partition(":")[0])
            c = self._crumb(holder) if holder else None
        return c.meta.get("sess", "") if c else ""

    def session(self, sid):
        """One session as nodes and edges (the graph that the explorer and the Desktop page draw).
        nodes: id, kind, title, tokens, meta, ranges, cues (sess, span), summary, use {accessed, uses}.
        edges: kid (a crumb -> each kid, in order), heritage (a derived crumb -> its source),
        nbr (a turn -> the turn it follows), call (a tool crumb -> the step crumb that called it)."""
        from .hermes import _ref_name
        with self.lock:
            top = self.refs.get(_ref_name("sess-", sid))
            if not top:
                raise KeyError(sid)
            ids, stack, seen = [], [top], set()
            while stack:
                cid = stack.pop()
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
                    stack.extend(reversed(self._crumb(cid).kids))
            sums = {cid: self.index.summary_of(cid) for cid in ids}
            ids += [x for x in dict.fromkeys(sums.values()) if x and x not in seen]
            heat = self.index.heat(ids)
            nodes, edges, known = [], [], set(ids)
            for cid in ids:
                c = self._crumb(cid)
                node = {"id": cid, "kind": c.kind, "title": c.title, "tokens": self.zoom.full(cid), "meta": c.meta,
                        "ranges": c.ranges}
                if c.kind in CUED:
                    node["cues"] = self.zoom.cue_words(cid).split()
                if sums.get(cid):
                    node["summary"] = sums[cid]
                if cid in heat:
                    node["use"] = {"accessed": heat[cid][0], "uses": heat[cid][1]}
                nodes.append(node)
                edges += [{"kind": "kid", "source": cid, "target": k, "w": 1.0} for k in c.kids]
                edges += [{"kind": "heritage", "source": cid, "target": h, "w": 1.0} for h in c.heritage]
                if c.meta.get("step") in known:
                    edges.append({"kind": "call", "source": cid, "target": c.meta["step"], "w": 1.0})
            order = {cid: i for i, cid in enumerate(ids)}
            marks = ",".join("?" * len(ids))
            for a, b in self.index.db.execute("SELECT a, b FROM nbr WHERE a IN (%s) AND b IN (%s)" % (marks, marks),
                                              ids + ids):
                if order[a] < order[b]:  # stored both ways: keep prev -> cur once, as cur follows prev
                    edges.append({"kind": "nbr", "source": b, "target": a, "w": 1.0})
            return {"session": sid, "root": top, "nodes": nodes, "edges": edges}

    def _band(self):
        """The controller's band from horizon.json, for receipts that do not record it (or None)."""
        try:
            with open(os.path.join(self.root, "horizon.json"), encoding="utf-8") as f:
                return json.load(f).get("band")
        except (OSError, ValueError):
            return None

    def _receipts(self, sid):
        """The context receipts of a session, oldest first: [(id, crumb)]."""
        from .hermes import _ref_name
        out, rid = [], self.refs.get(_ref_name("rcpt-", sid))
        while rid:
            c = self._crumb(rid)
            out.append((rid, c))
            rid = c.meta.get("prev") or None
        return out[::-1]

    def context(self, sid):
        """The Gantt of a session's context. events: one per receipt (recall, open, find, compress) with
        its turn, time, bytes, budget, ovf, rate, band and q (the query of a recall or a find, else "").
        With the turn budget: turn_budget (bytes; None when off), used (the bytes of recall, find and open
        in this turn so far, with the event), cap (the turn budget cut it) and want (its bytes before the
        cut; = bytes when not cut).
        turns: [{turn, crumb, ts}] for each user turn. rows: [{id, kind, label, session, bars}]; session
        is the sid of the crumb (of the crumb that holds a text; "" when not known). A bar is one copy of a
        crumb in the context:
        {from, to, t0, t1, level, bytes, ev}. from/to are event indexes, t0/t1 turns; to and t1 are None
        while the copy is live. A bar ends at the next compress event. The session's own crumbs are rows
        too (ev "own", level 1): from their turn to the first compress after they were stored."""
        from .hermes import _ref_name
        with self.lock:
            chain = self._receipts(sid)
            sess = self.refs.get(_ref_name("sess-", sid))
            if not chain and not sess:
                raise KeyError(sid)
            band = self._band()
            events, spent = [], {}
            for rid, c in chain:
                m = c.meta
                if m["ev"] != "compress":  # the turn budget counts recall, find and open
                    spent[m["turn"]] = spent.get(m["turn"], 0) + m["bytes"]
                events.append({"id": rid, "ev": m["ev"], "turn": m["turn"], "ts": m["ts"], "bytes": m["bytes"],
                               "budget": m.get("budget", 0), "ovf": m.get("ovf", False), "rate": m.get("rate", 0.0),
                               "band": m.get("band") or band, "q": m.get("q", ""), "cap": bool(m.get("cap", False)),
                               "used": m.get("used", spent.get(m["turn"], 0)), "turn_budget": m.get("tb") or None,
                               "want": m.get("want", m["bytes"])})
            stops = [i for i, e in enumerate(events) if e["ev"] == "compress"]
            rows, turns, t = {}, [], -1
            for cid in leaves(self.store, sess) if sess else []:
                c = self._crumb(cid)
                if c.meta.get("role") == "user":
                    t += 1
                    turns.append({"turn": t, "crumb": cid, "ts": c.meta.get("ts", 0)})
                j = next((k for k in stops if events[k]["turn"] > max(t, 0)), None)  # turns, not seconds
                rows.setdefault(cid, self._row(cid))["bars"].append(
                    {"from": None, "to": j, "t0": max(t, 0), "t1": None if j is None else events[j]["turn"],
                     "level": 1.0, "bytes": self.zoom.size(cid), "ev": "own"})
            for i, (_, c) in enumerate(chain):
                m = c.meta
                j = next((k for k in stops if k > i), None)
                for ref, lv, b in zip(m["items"], m["lv"], m["b"]):
                    rows.setdefault(ref, self._row(ref))["bars"].append(
                        {"from": i, "to": j, "t0": m["turn"], "t1": None if j is None else events[j]["turn"],
                         "level": lv, "bytes": b, "ev": m["ev"]})
            return {"session": sid, "events": events, "turns": turns, "rows": list(rows.values())}

    def venn(self, sid, at=None):
        """The sets of a session's context at one event (default: the last), since the compress before
        it: recalled, found and opened crumbs. Each set and each region has its ids (exact) and its bytes
        in the context (every copy counts). The regions split the union of the sets; the "by" of a region
        gives the bytes of each set's copies, so a drawing can scale each overlap."""
        from .hermes import _ref_name
        with self.lock:
            chain = self._receipts(sid)
            if not chain:
                if not self.refs.get(_ref_name("sess-", sid)):
                    raise KeyError(sid)
                return {"session": sid, "at": None, "regions": [],
                        "sets": {n: {"ids": [], "bytes": 0} for n in ("recalled", "found", "opened")}}
            at = len(chain) - 1 if at is None else int(at)
            if not 0 <= at < len(chain):
                raise ValueError("at: an event index from 0 to %d" % (len(chain) - 1))
            start = max([i for i in range(at + 1) if chain[i][1].meta["ev"] == "compress"], default=-1) + 1
            names = {"recall": "recalled", "find": "found", "open": "opened"}
            sets = {n: {"ids": [], "bytes": 0} for n in ("recalled", "found", "opened")}
            per = {}  # id -> {set name: bytes}
            for _, c in chain[start:at + 1]:
                name = names.get(c.meta["ev"])
                if not name:
                    continue
                for ref, b in zip(c.meta["items"], c.meta["b"]):
                    if ref not in sets[name]["ids"]:
                        sets[name]["ids"].append(ref)
                    sets[name]["bytes"] += b
                    per.setdefault(ref, {}).setdefault(name, 0)
                    per[ref][name] += b
            regions = {}
            for ref, by in per.items():
                key = tuple(sorted(by))
                reg = regions.setdefault(key, {"in": list(key), "ids": [], "bytes": 0, "by": dict.fromkeys(key, 0)})
                reg["ids"].append(ref)
                reg["bytes"] += sum(by.values())
                for name, b in by.items():
                    reg["by"][name] += b
            return {"session": sid, "at": at, "sets": sets, "regions": [regions[k] for k in sorted(regions)]}

    def ace(self, sid=None, per=0):
        """The size of the context over time, from <root>/ace.log (ACE's own record of every request).
        current: the session of the newest line; session: the one shown (sid, else the current one; a log line
        holds a prefix of the session id, so sid matches by prefix; "" when the log has none of it).
        points: one per request {i, ts, act, raw, sent, seen, base, crumbs, lay, folds, target, drop}: raw is the
        request before ACE, sent the request after it, seen the model's own count of what was sent. A fold shows
        as a point whose act is fold (or ratchet, when the target rose in place of a fold) and a drop of sent.
        candles: open, high, low, close of sent over runs of `per` requests. mix: the last request split into
        base, layer, exact and held. all: the totals of every session in the log (ace_totals). Reads only."""
        with self.lock:
            by = {}
            for r in ace_requests(self.root):
                by.setdefault(r["sess"], []).append(r)
            by.pop("-", None)  # a request that no session owns
            series = {sess: ace_series(rows) for sess, rows in by.items()}
            newest = max(series, key=lambda k: series[k][-1]["ts"], default="")
            want = str(sid or "")
            hit = want if want in series else max((k for k in series if want.startswith(k) or k.startswith(want)),
                                                   key=len, default="") if want else newest
            pts = series.get(hit, [])
            return {"current": newest, "session": hit, "target": next((p["target"] for p in reversed(pts) if p["target"]), None),
                    "points": pts, "candles": ace_candles(pts, per), "mix": ace_mix(pts), "all": ace_totals(series)}

    def close(self):
        """Release the store and index files (a second call does nothing)."""
        with self.lock:
            if not self._closed:
                self._closed = True
                self.store.close()
                self.index.close()

    def trail(self):
        try:
            with open(os.path.join(self.root, "trail.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return Trail().to_dict()


def cue_edges(nodes, per_node=4):
    """Edges between nodes that share cue words; weight = Jaccard; at most per_node edges each."""
    sets = [{w.lower() for w in n["cues"]} for n in nodes]
    pairs = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            shared = sets[i] & sets[j]
            if shared:
                pairs.append((len(shared) / len(sets[i] | sets[j]), i, j, sorted(shared)))
    pairs.sort(key=lambda p: -p[0])
    count, out = [0] * len(nodes), []
    for w, i, j, shared in pairs:
        if count[i] < per_node and count[j] < per_node:
            count[i] += 1
            count[j] += 1
            out.append({"source": nodes[i]["id"], "target": nodes[j]["id"], "kind": "cue", "w": round(w, 4),
                        "shared": shared})
    return out


def open_view(root):
    return View(root)


API = {
    "/api/graph": lambda v, q: v.graph(root=q.get("root") or None),
    "/api/crumb": lambda v, q: v.crumb(q["id"], min(1.0, max(0.0, float(q.get("level", "0.25"))))),
    "/api/find": lambda v, q: v.find(q.get("q", ""), max(1, min(50, int(q.get("n", "8"))))),  # k is the token
    "/api/trail": lambda v, q: v.trail(),
    "/api/path": lambda v, q: v.path(q["id"], within=q.get("within") or None),
    "/api/session": lambda v, q: v.session(q["sid"]),
    "/api/context": lambda v, q: v.context(q["sid"]),
    "/api/venn": lambda v, q: v.venn(q["sid"], q.get("at") or None),
    "/api/ace": lambda v, q: v.ace(q.get("sid") or None, max(0, min(500, int(q.get("per", "0"))))),
}


def make_server(root, port=8765, host="127.0.0.1"):
    """A read-only HTTP server for the page and its API (one View shared by every request)."""
    view, key = View(root), token(root)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    return self._send(200, "text/html; charset=utf-8", f.read())
            if url.path not in API:
                return self._send(404, "application/json", b'{"error": "not found"}')
            if not hmac.compare_digest(q.get("k", ""), key):
                return self._send(403, "application/json", b'{"error": "token"}')
            try:
                body = json.dumps(API[url.path](view, q), ensure_ascii=False).encode("utf-8")
            except KeyError as e:
                return self._send(404, "application/json", json.dumps({"error": "unknown id: %s" % e}).encode())
            except (ValueError, TypeError) as e:
                return self._send(400, "application/json", json.dumps({"error": str(e)}).encode())
            self._send(200, "application/json; charset=utf-8", body)

        def _send(self, code, kind, body):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")  # the page may sit in a sandboxed frame
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 (the base class name)
            pass

    class Server(ThreadingHTTPServer):
        def server_close(self):
            super().server_close()
            view.close()

    return Server((host, port), Handler)
