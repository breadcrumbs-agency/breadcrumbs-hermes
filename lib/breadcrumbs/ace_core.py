"""ACE core: the breadcrumb layer that folds an old chat into crumbs, for any harness.

Engine keeps the state of one chat and decides each request: pass, keep, ratchet or fold. A harness gives it the
messages of a request in the chat-completions shape (role, content, tool_calls), the model's own counts, and the dials
of the model as a plain dict (DIAL_DEFAULTS names them). It subclasses Engine for its session and model callbacks
(breadcrumbs_hermes/ace.py). This module reads no config file and imports no harness.
"""
import bisect
import collections
import heapq
import json
import math
import os
import re
import time

from . import dials, tags
from .index import STOP
from .ingest import call_text, plain, step_text
from .lines import put_text, split_lines
from .store import Store
from .zoom import GAP, HANDLE, Zoom


LEVELS = (0.0, 0.05, 0.1, 0.25, 0.5, 1.0)
HANDLE_BYTES, GROUP_BYTES = 40, 96  # one handle line; one folded group line
GROUP_TURNS = 8                     # turns in one group: the level of the tree above the messages
RARE_IDF = 2.0                      # a word is rare in a layer when its idf (ln of crumbs over crumbs that hold it) is this much
TARGET_TOKENS = 200_000
TAIL_SHARE, LAYER_SHARE, MIN_TAIL = 0.5, 0.4, 2  # the largest shares of a fold: exact tail, layer
SMALL_TARGET, SMALL_SHARES = 16384, (0.85, 0.15)  # the shares at a target of at most SMALL_TARGET tokens
FOLD_TO = 0.9                       # a fold makes the request this share of the target (a 10% cut)
TPB, TPB_RANGE = 0.3, (0.1, 1.0)    # tokens per byte until the model's first count, and its bounds
RATCHET = 1.45                      # a fold interval whose tool calls did no work raises the target (200k -> 290k)
GAIN_RANGE, FOLD_CAP = (0.5, 1.5), 1.35  # the bounds of a fold's gain; a fold aims at most at FOLD_CAP of the
#                                          target by the estimate: FOLD_TO 0.9 needs 1.27 at the live gain of 0.709
WORK_TOOLS = frozenset(("write_file", "patch", "skill_manage", "delegate_task", "cronjob", "send_message",
                        "text_to_speech", "image_generate"))  # a call to one of these is work
MEMORY_TOOLS = frozenset(("crumb", "crumb_find"))  # ACE asks the model to open crumbs after a fold: that is no different tool
WRITE_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:commit|merge|push|apply|am|cherry-pick|rebase|revert|tag|stash|mv|rm)\b"
                      r"|(?:^|[\s;&|(])(?:mv|cp|rm|mkdir|touch|tee|ln|install|rsync|scp|ffmpeg)\s|\bsed\s+-i"
                      r"|\bopen\([^)]*['\"][wax]b?\+?['\"]|\.write(?:_text|_bytes)?\(|\b(?:write_file|patch)\(")
REDIRECT_RE = re.compile(r"(?<![\d&>=-])>>?\s*(?![&=\d]|/dev/null)[\w./~$\"'{]")  # a shell writes a file
LOG_BYTES = 1 << 20                 # ace.log turns over to ace.log.1 at this size
LOG_SESS = 24                       # a log line holds this many characters of the session id
JOIN_SECONDS = 120                  # join(): a line's request starts at most this many seconds after it
JOIN_TICK = 1                       # join(): the step of the stamps of ace.log and usage.log (whole seconds)
HALF = 12                           # recency half-life, in turns
ROLE_WEIGHT = {"user": 2.0, "assistant": 1.5, "tool": 1.0, "call": 0.5}
CRUMB_TOOLS = ("crumb", "mcp__crumb")

LAYER_HEAD = "## Breadcrumb layer (ACE)\n"
LAYER_NOTE = ("Earlier turns of this chat, condensed. Each %s line is a crumb at a level from 0 (handle) to 1 "
              "(exact text); \u25c6 marks one that the new message needs. When one is too thin for the task, open "
              "it with crumb(ref, level): the next layer gives it more room and shrinks the crumbs around it.\n"
              % HANDLE)
REF_RE = re.compile(r"^%s ([0-9a-f]{8}(?:\.\d+)?:\d+-\d+) " % HANDLE, re.M)
WORD_RE = re.compile(r"[a-z][a-z0-9_]{2,}")
ANCHOR_RE = re.compile(r"[\w./-]*\d[\w./-]*")
LOG_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d pid=\d+ sess=(.*) act=\w+ in=\d+ out=\d+ last=\d+ "
                    r"base=(\d{1,12}|None) tpb=(\d\.\d{4}) r=(\d\.\d{4}) folds=\d+ layer=\d+"
                    r"(?: target=(\d{1,9}) gain=(\d\.\d{4})(?: lay=(\d{1,9})"
                    r"(?: gear=(lock|low|-) think=\d{1,12} calls=\d{1,9} work=[01] room=(\d{1,12}))?)?)?")  # a line of Engine._log()

# The dials of a model with their defaults: the ACE rows of the one table (dials.DIALS). Engine.dial_values reads
# their values for a model; a missing key has its default here.
DIAL_DEFAULTS = {d.key: d.default for d in dials.DIALS if d.key.startswith("ace_")}


# -- messages ----------------------------------------------------------------------------------------------
def _content(m):
    c = m.get("content")
    if isinstance(c, list):
        c = "\n".join(p["text"] if isinstance(p, dict) and isinstance(p.get("text"), str)
                      else "[%s]" % (p.get("type", "part") if isinstance(p, dict) else "part") for p in c)
    return c if isinstance(c, str) else ("" if c is None else json.dumps(c, ensure_ascii=False))


def _calls_of(m):
    fs = [(c.get("function") or {}) if isinstance(c, dict) else {} for c in m.get("tool_calls") or []]
    return [(f.get("name", "?"), f.get("arguments", "")) for f in fs]


def message_text(m):
    """The text of a message as the model sees it: its content, then one line per tool call. It sizes the
    request; crumb_text is what a crumb keeps."""
    text = _content(m)
    for name, args in _calls_of(m):
        text += ("\n" if text else "") + "\u2192 %s %s" % (name, args)
    return text


def crumb_text(m):
    """The text a crumb keeps of a message: a tool result and the arguments of calls as plain text, not
    JSON (ingest.plain, ingest.step_text). For a tool result or a step, the provider keeps the same bytes."""
    text, calls = _content(m), _calls_of(m)
    if calls:
        return step_text(text, calls)
    return plain(text) if m.get("role") == "tool" else text


def reasoning_text(m):
    """The thinking that goes back to the model with an assistant message, once. A message keeps it in up to three
    fields (reasoning_details, reasoning, reasoning_content); a signature is not text, and the model's count does not
    hold it."""
    d = m.get("reasoning_details")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = None
    if isinstance(d, list) and d:
        return "".join(str(x.get("thinking") or x.get("text") or "") for x in d if isinstance(x, dict))
    r = m.get("reasoning") or m.get("reasoning_content")
    return r if isinstance(r, str) else ""


def request_bytes(messages):
    """The size of a request in bytes: text, tool calls and thinking (select_context turns it into tokens)."""
    return sum(len(message_text(m).encode("utf-8")) + len(reasoning_text(m).encode("utf-8")) + 16
               for m in messages)


def cut(turns, room):
    """(b, k): keep turns[b][0], turns[b][1 + k:] and turns[b + 1:]; the rest folds. None: nothing folds.

    From the newest message back, whole turns stay while they fit in room. In the first turn that does
    not fit, its user message stays (the layer goes there) with its newest steps that fit, back to a step
    start (an assistant message). The newest step always stays. When no step of an older turn fits, that
    turn folds whole."""
    used, last = 0, len(turns) - 1
    for t in range(last, -1, -1):
        turn = turns[t]
        if used + request_bytes(turn) <= room:
            used += request_bytes(turn)
            continue
        keep, used = len(turn), used + request_bytes(turn[:1])
        for i in range(len(turn) - 1, 0, -1):
            if turn[i].get("role") != "assistant":
                continue
            step = request_bytes(turn[i:keep])
            if used + step > room and not (t == last and keep == len(turn)):
                break
            used, keep = used + step, i
        if keep == len(turn) and t < last:
            t, keep = t + 1, 1
        return None if t == 0 and keep == 1 else (t, keep - 1)
    return None


def split(messages):
    """(head, turns): the messages before the first user message, then one list per user message."""
    i = next((k for k, m in enumerate(messages) if m.get("role") == "user"), len(messages))
    turns = []
    for m in messages[i:]:
        if m.get("role") == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    return messages[:i], turns


def ref_of(store, text):
    """The crumb ref of a text: its document and its line range (the text is stored if it is new)."""
    data = text.encode("utf-8")
    return "%s:0-%d" % (put_text(store, data), len(split_lines(data)))


def _words(text):
    """The distinct words of text in the order of first use: the same in every process (a set changes its order with
    Python's hash seed, and with it the cue words of a layer and so the text of the request)."""
    return tuple(dict.fromkeys(w for w in WORD_RE.findall(text[:4000].lower()) if w not in STOP))


def _anchors(text):
    return {a.lower() for a in ANCHOR_RE.findall(text[:4000]) if len(a) >= 4}


# -- the budget --------------------------------------------------------------------------------------------
def cost(e, level):
    """Bytes of one crumb at a level (None: inside a folded group)."""
    if level is None:
        return 0
    return HANDLE_BYTES + (int(level * e["size"]) if level > 0 else 0)


def layer_cost(entries, levels):
    folded = {e["group"] for e in entries if levels[e["id"]] is None}
    return GROUP_BYTES * len(folded) + sum(cost(e, levels[e["id"]]) for e in entries)


def _up(level):
    return next((v for v in LEVELS if v > level), None)


def allocate(entries, budget):
    """A level for each entry (dicts: id, group, size, weight, floor) inside budget bytes.

    While the handles do not fit, the oldest groups fold (level None), the groups of opened crumbs last.
    Opened crumbs (floor > 0) come next, at their floor or as near as fits, and the other crumbs of their
    group lose half their weight. Then each step of LEVELS goes to the crumb that gains most per byte; the
    value of a level is weight * sqrt(level), so the room spreads over many crumbs before one gets its full
    text.
    """
    count = collections.Counter(e["group"] for e in entries)
    opened = {e["group"] for e in entries if e.get("floor")}
    shown, folded, hidden = len(entries) * HANDLE_BYTES, 0, set()
    for g in sorted(count, key=lambda g: (g in opened, g)):
        if shown + folded <= budget:
            break
        hidden.add(g)
        shown, folded = shown - count[g] * HANDLE_BYTES, folded + GROUP_BYTES
    levels = {e["id"]: (None if e["group"] in hidden else 0.0) for e in entries}
    room = budget - shown - folded
    live = [e for e in entries if e["group"] not in hidden]
    grown = {e["group"] for e in live if e.get("floor")}
    weight = {e["id"]: e["weight"] * (0.5 if e["group"] in grown and not e.get("floor") else 1.0) for e in live}
    for e in sorted((e for e in live if e.get("floor")), key=lambda e: -e["weight"]):
        want = next((v for v in LEVELS if v >= e["floor"]), 1.0)
        while want > 0 and cost(e, want) - HANDLE_BYTES > room:
            want = max(v for v in LEVELS if v < want)
        room -= cost(e, want) - HANDLE_BYTES
        levels[e["id"]] = want
    heap = []

    def push(i):
        e = live[i]
        cur = levels[e["id"]]
        nxt = _up(cur)
        if nxt is not None and e["size"] > 0 and not e.get("by"):  # a superseded crumb stays a handle
            gain = weight[e["id"]] * (nxt ** 0.5 - cur ** 0.5)
            heapq.heappush(heap, (-gain / max(1.0, (nxt - cur) * e["size"]), i))

    for i in range(len(live)):
        push(i)
    while heap:
        _, i = heapq.heappop(heap)
        e = live[i]
        cur = levels[e["id"]]
        extra = cost(e, _up(cur)) - cost(e, cur)
        if extra <= room:
            room -= extra
            levels[e["id"]] = _up(cur)
            push(i)
    return levels


def _logged(root, session):
    """The whole lines of this session in <root>/ace.log and then ace.log.1, newest first, as LOG_RE matches."""
    sess = str(session or "")[:LOG_SESS]
    if not sess:  # no session id: no line is known to be of this chat
        return
    key = " sess=%s act=" % sess
    for name in ("ace.log", "ace.log.1"):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError:
            continue
        for line in reversed(lines[:-1]):  # the text after the last line end is not a whole line
            m = LOG_RE.fullmatch(line) if key in line else None
            if m and m.group(1) == sess:
                yield m


def status_lines(root, n=6):
    """The newest line of each of the n newest chats in <root>/ace.log: when, the chat, what ACE did, the size of
    its request in tokens, what ACE sent (out) and the target it holds, and a mark when what it sent is over the
    target. The gear (lock or low) follows the folds when the chat has a gearbox. A model sees only its own
    context, not the size of it: this is where it reads the size."""
    last = {}
    for name in ("ace.log.1", "ace.log"):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")[:-1]
        except OSError:
            continue
        for line in lines:
            m = re.match(r"(\S+) pid=\d+ sess=(\S*) act=(\w+) in=(\d+) out=(\d+) .* folds=(\d+) layer=\d+ target=(\d+) ", line)
            if m:
                g = re.search(r" gear=(lock|low) ", line)
                last[m.group(2)] = m.groups() + (g.group(1) if g else "",)
    out = []
    for ts, sess, act, size, sent, folds, target, gear in sorted(last.values())[::-1][:n]:
        size, sent, target = int(size), int(sent), int(target)
        over = f"  OVER by {sent - target:,}" if sent > target else ""
        out.append(f"{ts}  {sess:<24} {act:<9} in {size:>9,}  sent {sent:>9,}  target {target:>9,}  "
                   f"{100 * sent // max(1, target):>3}%  folds {folds}{'  gear ' + gear if gear else ''}{over}")
    return out


def join(lines, requests):
    """Each line of ace.log with the model request that it shaped: the first main request (usage.log, task main) of
    its session that starts at or after the line, and starts within JOIN_SECONDS of it. ace.log writes its line at the
    START of a request and usage.log at the END, so the start of a request is its stamp less its dur. Both stamps
    are whole seconds (cut down), so a start can show as up to JOIN_TICK before the stamp of the line written before
    it: a request joins when its start + JOIN_TICK is after the line. (By end time, a line written in the second that
    request R ended joined R, not the request after R that it shaped.) A request with no dur starts at its stamp.
    A line holds the session id cut to LOG_SESS characters and a request the whole id, so they join by that prefix.
    lines: rows with sess, ts (and act); requests: rows with sess, ts, task (and req, dur), as the viewer reads the
    two logs. Requests of one second are ordered by req id, then as given.
    -> [(line, request)] in the order of lines; a line with no such request is left out. The port ace_join."""
    seqs = {}
    for n, r in enumerate(requests):
        if (r.get("task") or "main") == "main":
            seqs.setdefault(str(r["sess"])[:LOG_SESS], []).append((r["ts"], r.get("req") or "", n, r))
    times = {}
    for key, seq in seqs.items():
        seq.sort(key=lambda e: e[:3])
        times[key] = [e[0] for e in seq]
    out = []
    for line in lines:
        key = str(line["sess"])[:LOG_SESS]
        if key in ("", "-") or key not in seqs:  # "-": a line of an engine with no session id
            continue
        # a request that ends JOIN_TICK or more before the line also starts before it: skip those by the end stamp
        for ts, _, _, r in seqs[key][bisect.bisect_right(times[key], line["ts"] - JOIN_TICK):]:
            start = ts - float(r.get("dur") or 0)
            if start + JOIN_TICK > line["ts"]:
                if start - line["ts"] <= JOIN_SECONDS:
                    out.append((line, r))
                break
    return out


def logged_counts(root, session, target):
    """The model's counts that an engine object wrote to ace.log for this session: (base, tpb, ratio), or None.
    The newest whole line of the session counts, from <root>/ace.log and then from ace.log.1. A line is ignored
    when it is damaged, when it has no line end, when it has no base (an object with no count wrote it), and
    when it holds a tpb or a ratio outside TPB_RANGE or a base at or above the target."""
    for m in _logged(root, session):
        if m.group(2) != "None":
            base, tpb, ratio = int(m.group(2)), float(m.group(3)), float(m.group(4))
            if base < target and all(TPB_RANGE[0] <= x <= TPB_RANGE[1] for x in (tpb, ratio)):
                return base, tpb, ratio
    return None


def logged_ratchet(root, session):
    """The raised target and the gain of the newest line of this session that holds them, or None."""
    for m in _logged(root, session):
        if m.group(5):
            return int(m.group(5)), min(GAIN_RANGE[1], max(GAIN_RANGE[0], float(m.group(6))))
    return None


def logged_gear(root, session):
    """The gear and the room of the newest line of this session that holds them, or None (a line from before the
    gears does not)."""
    for m in _logged(root, session):
        if m.group(8):
            return m.group(8), int(m.group(9))
    return None

def tool_name(call):
    """The name of a tool call without its prefix (mcp__write_file is write_file)."""
    f = (call.get("function") or {}) if isinstance(call, dict) else {}
    return str(f.get("name") or "").rsplit("__", 1)[-1]


def is_work(call):
    """True when a tool call writes: a WORK_TOOLS call, or a shell command or code that writes a file or makes
    a commit (WRITE_RE; a redirect to a file in a shell). A read, a search and a test run are no work."""
    f = (call.get("function") or {}) if isinstance(call, dict) else {}
    name = tool_name(call)
    if name in WORK_TOOLS:
        return True
    if name not in ("terminal", "execute_code"):
        return False
    raw = f.get("arguments") or ""
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        args = raw
    text = "\n".join(v for v in args.values() if isinstance(v, str)) if isinstance(args, dict) else str(raw)
    return bool(WRITE_RE.search(text) or (name == "terminal" and REDIRECT_RE.search(text)))


def _call_key(c):
    f = c.get("function") or {}
    return str(c.get("id") or "") or "%s %s" % (f.get("name"), f.get("arguments"))


def calls_of(turns):
    """The keys of the tool calls in turns: the call id, else the name and the arguments."""
    return {_call_key(c) for t in turns for m in t for c in (m.get("tool_calls") or []) if isinstance(c, dict)}


def turn_worked(turn):
    """True when a call in turn (one user turn: a list of messages) writes."""
    return any(is_work(c) for m in turn for c in m.get("tool_calls") or [] if isinstance(c, dict))



# -- the engine --------------------------------------------------------------------------------------------

class Engine:
    """The breadcrumb layer of one chat: the state of ACE and its decisions (see the module doc). A harness subclasses
    it for its session and model callbacks."""
    threshold_percent = 0.75  # the share of the context window at which a request folds whatever the target is

    def __init__(self, target_tokens=None, name="breadcrumbs"):
        env = os.environ.get("BREADCRUMBS_ACE_TOKENS", "")
        self._given = bool(target_tokens or env.isdigit())  # else <root>/config.json "ace_tokens" may set it
        self.target_tokens = int(target_tokens or (env if env.isdigit() else 0) or TARGET_TOKENS)
        self._name, self.tpb, self._sent, self.session, self.root = name, TPB, 0, "", None
        self.child = False  # a child agent (a harness sets it; see _cannot_open)
        self.base, self.ratio = None, TPB  # see tokens(): the two readings of the model's own counts
        self.last_prompt_tokens = self.last_completion_tokens = self.last_total_tokens = 0
        self.threshold_tokens = self.context_length = self.compression_count = 0
        self.home, self.store, self.zoom, self._index = None, None, None, None
        self.levels, self.layer, self.boundary, self.folds, self.layer_crumbs = {}, "", None, 0, 0
        self.ratchet, self.gain, self._calls, self._fold_est = 1.0, 1.0, None, 0  # see _raise() and _fold()
        self._fold_bytes = 0  # the bytes that the last fold aimed at
        self._landed: tuple[float, float] | None = None  # the count of the last folded request, and those bytes
        self.model, self.fold_to, self.fold_cap, self.step = "", FOLD_TO, FOLD_CAP, RATCHET  # see _apply()
        self.diverse, self.ceiling = 4, 0  # ace_diverse and ace_ceiling (DIAL_DEFAULTS; a ceiling of 0 is none)
        self.lockup, self.room, self.downshift, self.turn_fold, self.persist = False, 0, 0.0, False, 0.0  # see _apply()
        self.gear, self.room_now, self._seen = "-", 0, (0, 0)  # the gear ("-": no gearbox), its room in tokens, _interval()
        self._turn_n, self._resumed, self._room_logged = None, False, 0  # see _shift() and _open()

    @property
    def name(self):
        return self._name

    # -- the model's counts ------------------------------------------------------------------------------
    def counted(self, prompt, completion, total):
        """The model's own counts (tokens) of the request that it answered: the prompt, the completion and the total."""
        self.last_prompt_tokens, self.last_completion_tokens, self.last_total_tokens = prompt, completion, total
        if self.last_prompt_tokens > 0 and self._sent > 0:  # the model's own count of the request ACE sent
            p, b = self.last_prompt_tokens, self._sent
            if self._fold_est:  # the count of the request that a fold made, over the estimate that it aimed at
                self.gain = min(GAIN_RANGE[1], max(GAIN_RANGE[0], p / self._fold_est))
                self._fold_est, self._landed = 0, (p, self._fold_bytes)  # _fold() measures it again
            self.ratio = min(TPB_RANGE[1], max(TPB_RANGE[0], p / b))
            if self.base is None:  # the first count: what the bytes at TPB do not hold is the fixed part
                self.base = max(0, int(p - self.tpb * b))
            elif p > self.base:  # later counts: the tokens per byte of the rest
                self.tpb = min(TPB_RANGE[1], max(TPB_RANGE[0], (p - self.base) / b))

    def tokens(self, nbytes):
        """Tokens of a request of nbytes, by the model's own counts: the larger of two readings. One is the
        whole last count per byte. The other is a fixed part (the tool schemas, which the bytes do not hold;
        from the first count) and the tokens per byte of the rest. On a live bcdog run (Nov 8 2026, 14
        requests) the larger reading was at most 5.9% under the real count; each one alone, 21.5% and 22.3%."""
        return max(self.ratio * nbytes, (self.base or 0) + self.tpb * nbytes)

    def _bytes_for(self, tokens):
        return max(0.0, min(tokens / self.ratio, (tokens - (self.base or 0)) / self.tpb))

    def should_compress(self, prompt_tokens=None):
        n = self.last_prompt_tokens if prompt_tokens is None else prompt_tokens
        return bool(self.threshold_tokens) and (n or 0) >= self.threshold_tokens

    def _dial(self):
        """This model's dials: dial_values(root, model), applied by _apply."""
        self._apply(self.dial_values(self.root, self.model))

    def dial_values(self, root, model):
        """The dials of model in the folder root, read by dials.py: the top level of <root>/config.json, the built-in
        entries and each entry of "models" whose glob matches the model. A dial that is missing or not valid has its
        default. The environment is left out: the constructor reads the one variable of ACE."""
        cfg = dials.config(root)
        return {key: dials.value(cfg, model, key, {}) for key in DIAL_DEFAULTS}

    def use_model(self, model, context_length):
        """The model of the chat and its context window in tokens: the dials of that model, and the threshold (the
        share threshold_percent of the window) that no target passes."""
        self.model = str(model or "")
        if self.root:  # a model switch inside a session takes the dials of the new model
            self._dial()
        self.context_length = int(context_length or 0)
        self.threshold_tokens = int(self.context_length * self.threshold_percent)

    def _apply(self, values):
        """Set the dials from a dict of their values (valid ones; DIAL_DEFAULTS for a key that is missing). The target
        is ace_tokens, unless the constructor gave one."""
        v = {**DIAL_DEFAULTS, **{k: x for k, x in values.items() if k in DIAL_DEFAULTS}}
        if not self._given:
            self.target_tokens = v["ace_tokens"]
        self.diverse, self.ceiling = v["ace_diverse"], v["ace_ceiling"]
        self.fold_to, self.fold_cap, self.step = v["ace_fold_to"], v["ace_fold_cap"], v["ace_ratchet"]
        self.lockup, self.room = v["ace_lockup"], v["ace_room"]  # the gears (see _shift)
        self.downshift, self.turn_fold, self.persist = v["ace_downshift"], v["ace_turn_fold"], v["ace_persist"]
        self.gear = ("lock" if self.gear == "-" else self.gear) if self.lockup else "-"

    def _ratcheted(self):
        """The target that reading has earned: the band times the ratchet, at most the model's ace_ceiling."""
        t = int(round(self.target_tokens * self.ratchet))
        return min(t, self.ceiling) if self.ceiling else t

    def _target(self, budget_tokens=0):
        return min(t for t in (self._ratcheted(), self.threshold_tokens, budget_tokens) if t) or TARGET_TOKENS

    def _raise(self, budget_tokens=0):
        """The target grows by the model's ratchet dial (RATCHET), while the threshold, the budget and the model's
        ace_ceiling let it grow.
        In lockup the room is the bound (and the target is already the room), so only a chat with no room known grows."""
        if self.lockup and self.room_now:
            return False
        old, was = self.ratchet, self._target(budget_tokens)
        self.ratchet = old * self.step
        if self._target(budget_tokens) > was:
            return True
        self.ratchet = old
        return False

    def _new_calls(self, turns):
        return [c for t in turns for m in t for c in (m.get("tool_calls") or [])
                if isinstance(c, dict) and _call_key(c) not in (self._calls or ())]

    def _busy(self, turns):
        """True when the tool calls since the last fold or raise are work and not reading: one writes, or they use
        ace_diverse different tools (a model that reads a lot of data uses few; a model that works uses many)."""
        if self._calls is None:
            return False
        return self._worked(self._new_calls(turns))

    def _worked(self, new):
        """True when new (tool calls) are work: one writes, or they use ace_diverse different tools. The memory
        tools do not count as different tools: ACE asks the model to open crumbs after a fold, and a read, a search,
        crumb and crumb_find made four, which kept a chat that only reads in the folding of work. In lockup only a
        write is work: research uses many tools."""
        if any(is_work(c) for c in new):
            return True
        if self.lockup:
            return False
        return bool(self.diverse) and len({tool_name(c) for c in new} - MEMORY_TOOLS) >= self.diverse

    def _interval(self, turns):
        """(calls, work) of the interval as a request finds it: the tool calls since the last fold or raise (all of
        them before the first), and 1 when they are work. The log keeps both."""
        new = self._new_calls(turns)
        return len(new), int(self._worked(new))

    def _taper(self, turns):
        """The room that reading took shrinks while the model works. Each interval of work (_busy) takes the fold
        share (10% at ace_fold_to 0.9) off the room above the band, down to the band: no jump back, and a fold
        comes only when the request passes the lower target. Reading grows the room (_raise), work tapers it."""
        if self.ratchet <= 1.0:
            return
        if self._calls is None:  # a chat that was resumed with a raised target: the interval starts now
            self._calls = calls_of(turns)
        elif self._busy(turns):
            self._step_down()
            self._calls = calls_of(turns)

    def _step_down(self):
        """One step of the taper: the share of the room above the band that an interval of work (a user turn of work,
        in low gear) takes off. ace_downshift sets it; 0 is 1 minus ace_fold_to, the fold share, as it always was."""
        room = (self.ratchet - 1.0) * (1.0 - self.downshift if self.downshift else self.fold_to)
        self.ratchet = 1.0 + room if room >= 0.005 else 1.0

    def _idle(self, turns):
        """True when the tool calls since the last fold or raise did no work: there are some, and none writes.
        A read and fold loop (read, fold, read again) shows that a fold keeps too little for the work. In lockup, no
        work since the last fold or raise is idle at once, also before the first fold and with no new call."""
        if self.lockup:
            return self.gear == "lock" and not self._seen[1]
        if self._calls is None:
            return False
        new = self._new_calls(turns)
        return bool(new) and not self._busy(turns)

    # -- the gears ---------------------------------------------------------------------------------------
    def _room_for(self, budget_tokens=0):
        """The research room in tokens, or 0 when none is known: ace_room, else the threshold (or the budget when
        the threshold is not known), else the room that this chat's log kept. Never under the band, and never over
        the threshold, the budget or the model's ace_ceiling: the room is a grant, and the harness's own limits stay."""
        ceiling = min((t for t in (self.threshold_tokens, budget_tokens) if t), default=0)
        room = self.room or ceiling or self._room_logged
        if not room:
            return 0
        room = max(room, self.target_tokens)
        return min(t for t in (room, ceiling, self.ceiling) if t)

    def _turn(self, turns):
        """True at the first request of a user turn: there are more or fewer turns than at the request before, or this
        object is new and the newest turn holds its question alone (an object that starts inside a turn is not at its
        start). A request that is sent again has the same turns, so it is no new turn."""
        n, before = len(turns), self._turn_n
        self._turn_n = n
        return len(turns[-1]) == 1 if before is None else n != before

    def _shift(self, turns, budget_tokens=0):
        """The gearbox, at each request in lockup. Lockup (research and reasoning): the target is the room from the
        first request, and thinking and reads keep it. Low gear (work): the first call that writes shifts down, and
        at the first request of each later user turn a turn of work takes ace_downshift of the room above the band
        (the target changes between turns and not inside one, so the layer and the prompt cache hold inside a turn).
        A user turn with no work puts the chat in lockup again, and its interval starts there. The target is the
        ratchet, as before: the room over the band. Returns True at the first request of a user turn."""
        turn = self._turn(turns)
        if self.gear == "low" and turn:
            if len(turns) > 1 and turn_worked(turns[-2]):
                self._step_down()
            else:
                self.gear, self._calls, self._seen = "lock", calls_of(turns), (0, 0)
        if self.gear == "lock" and self._seen[1]:
            self.gear = "low"
        self.room_now = self._room_for(budget_tokens)
        if self.gear == "lock" and self.room_now:
            self.ratchet = max(1.0, self.room_now / self.target_tokens)
        return turn

    def get_status(self):
        last = max(self.last_prompt_tokens, 0)
        return {"last_prompt_tokens": last, "threshold_tokens": self.threshold_tokens,
                "context_length": self.context_length,
                "usage_percent": min(100, last / self.context_length * 100) if self.context_length else 0,
                "compression_count": self.compression_count, "ace_target_tokens": self._target(self.context_length),
                "ace_folds": self.folds, "ace_layer_crumbs": self.layer_crumbs,
                "ace_layer_bytes": len(self.layer.encode("utf-8")), "ace_tokens_per_byte": round(self.tpb, 4),
                "ace_ratio": round(self.ratio, 4), "ace_base_tokens": self.base, "ace_gain": round(self.gain, 4),
                "ace_fold_to": self.fold_to, "ace_fold_cap": self.fold_cap, "ace_ratchet": self.step,
                "ace_gear": self.gear, "ace_room_tokens": self.room_now,
                "ace_sent_tokens": int(self.tokens(self._sent)) if self._sent else 0}

    def get_automatic_compaction_status_message(self, *, phase, default_message, **context):
        return "%s breadcrumbs ACE folded the older turns into %d crumbs (%d KB), no model call" % (
            HANDLE, self.layer_crumbs, len(self.layer.encode("utf-8")) // 1024)

    def has_content_to_compress(self, messages):
        return len(split(messages)[1]) > MIN_TAIL

    # -- sessions and the store --------------------------------------------------------------------------
    def start_session(self, session_id, home=None):
        """A new chat: the store of home (kept when home is None) and the state of the chat from its start."""
        self.close()
        self.home = home or self.home
        self.session = str(session_id or "")
        self.levels, self.layer, self.boundary = {}, "", None
        self.ratchet, self.gain, self._calls, self._fold_est = 1.0, 1.0, None, 0
        self._fold_bytes, self._landed = 0, None
        self.gear, self.room_now, self._seen = "-", 0, (0, 0)  # _open() reads the gear and the room back from the log
        self._turn_n, self._resumed, self._room_logged = None, False, 0

    def reset_session(self):
        self.levels, self.layer, self.boundary, self._calls = {}, "", None, None
        self.gear, self._turn_n = ("lock" if self.lockup else "-"), None

    def close(self):
        for obj in (self.store, self._index):
            try:
                obj and obj.close()
            except Exception:
                pass
        self.store = self.zoom = self._index = None

    def _open(self):
        if self.store is not None:
            return True
        root = os.path.join(self.home_folder(), "breadcrumbs")
        try:
            self.store = Store(os.path.join(root, "store"))
            self.root = root
            self._dial()  # the profile's own target and this model's dials, when its config.json has them
            if self.base is None:  # a new object has no count: it takes the counts that its session logged before
                counts = logged_counts(root, self.session, self._target())
                if counts:
                    self.base, self.tpb, self.ratio = counts
            if self.ratchet == 1.0:  # and the target that its session raised, and the gain of its last fold
                state = logged_ratchet(root, self.session)
                if state:
                    self.ratchet, self.gain = max(1.0, state[0] / self.target_tokens), state[1]
            if self.lockup:  # and its gear and room (a log from before the gears, or with no gearbox, starts in lockup)
                self._resumed = next(_logged(root, self.session), None) is not None  # the chat has lines in the log
                state = logged_gear(root, self.session)
                if state:
                    self.gear, self._room_logged = state[0] if state[0] != "-" else "lock", state[1]
            path = os.path.join(root, "index.db")
            if os.path.exists(path):
                from .index import Index
                self._index = Index(path, readonly=True)
            ix = self._index
            self.zoom = Zoom(self.store, summaries=ix and ix.summary_of, cues=ix and ix.signature,
                             words=ix and ix.tiers)
            return True
        except Exception:
            self.close()
            return False

    def home_folder(self):
        """The folder that holds the breadcrumbs folder of this chat: self.home (a harness sets it)."""
        return str(self.home or "")

    # -- the layer ---------------------------------------------------------------------------------------
    def _cannot_open(self, head):
        """True when the layer is a dead end for this request: a harness says so (breadcrumbs_hermes.ace). Here: never."""
        return False

    def select_context(self, request_messages, *, conversation_messages=None, incoming_message=None,
                       budget_tokens=0):
        head, turns = split(request_messages)
        if not turns or not self._open():
            return None
        if self._cannot_open(head):  # a layer is a dead end for this model: leave the request as it is
            self._sent, self._fold_est = request_bytes(request_messages), 0
            self._log("pass", self._sent)
            return None
        if self.lockup and self._calls is None:  # in lockup the interval starts with the chat (a resumed chat: now)
            self._calls = calls_of(turns) if self._resumed else set()
        self._seen = self._interval(turns)
        if self.lockup:
            turn = self._shift(turns, budget_tokens)
        else:
            turn = False
            self._taper(turns)
        limit = self._bytes_for(self._target(budget_tokens))  # the target in bytes, by the model's own counts
        size, out, act = request_bytes(request_messages), None, "pass"
        if turn and self.turn_fold and self.gear == "low" and self.layer:  # low gear: each layer follows the task
            bound = self._fold(head, turns, self._target(budget_tokens))
            if bound:
                out, act = self._assemble(head, turns, bound), "fold"
        if out is None:
            bound = self._kept(turns)
            kept = self._assemble(head, turns, bound) if bound is not None else None
            need = request_bytes(kept) if kept is not None else size
            if need > limit and self._idle(turns) and self._raise(budget_tokens):  # no work since the fold: more room
                limit, act, self._calls = self._bytes_for(self._target(budget_tokens)), "ratchet", calls_of(turns)
            if kept is not None and need <= limit:
                out, act = kept, "keep" if act == "pass" else act
            elif size > limit:
                bound = self._fold(head, turns, self._target(budget_tokens))
                out, act = (self._assemble(head, turns, bound), "fold") if bound else (None, act)
        self._sent = request_bytes(out) if out is not None else size
        if act != "fold" or self.base is None:  # only the count of a request that a fold made sets the gain
            self._fold_est = 0
        self._log(act, size, out is not None, request_messages)
        return out

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False, memory_context=""):
        head, turns = split(messages)
        if not turns or not self._open():
            return messages
        if self.lockup:
            if self._calls is None:
                self._calls = calls_of(turns) if self._resumed else set()
            self._seen = self._interval(turns)
            self._shift(turns, self.context_length)
        else:
            self._taper(turns)
        bound = self._fold(head, turns, self._target(self.context_length))
        self._fold_est = 0
        if bound is None:
            return messages
        self.compression_count += 1
        self.boundary = None  # the stored history now starts with the layer; the next fold reads it as a turn
        return self._assemble(head, turns, bound)

    def _log(self, act, size, layered=False, request=()):
        """One line per request in <root>/ace.log: what ACE did, the sizes in tokens by the model's counts, and
        the model's own count of the request before (last). lay is the layer's share of the request in tokens
        (0 when the request holds no layer); the explorer splits the context into base, lay and the exact rest.
        The last fields are the gearbox's: gear (lock, low, or - with no gearbox), think (the thinking in the
        request before ACE, in tokens), calls (the tool calls since the last fold or raise, as the request found
        them), work (1 when they are work) and room (the research room that the chat holds, in tokens; 0: none)."""
        path = os.path.join(self.root, "ace.log")
        lay = int(self.tokens(len(self.layer.encode("utf-8")))) if layered and self.layer else 0
        think = int(self.tpb * sum(len(reasoning_text(m).encode("utf-8")) for m in request))
        calls, work = self._seen
        line = ("%s pid=%d sess=%s act=%s in=%d out=%d last=%d base=%s tpb=%.4f r=%.4f folds=%d layer=%d target=%d "
                "gain=%.4f lay=%d gear=%s think=%d calls=%d work=%d room=%d\n") % (
            time.strftime("%Y-%m-%dT%H:%M:%S"), os.getpid(), (self.session or "-")[:LOG_SESS], act, self.tokens(size),
            self.tokens(self._sent), self.last_prompt_tokens, self.base, self.tpb, self.ratio, self.folds,
            self.layer_crumbs, self._ratcheted(), self.gain, lay, self.gear, think, calls, work, self.room_now)
        try:
            if os.path.getsize(path) > LOG_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _kept(self, turns):
        if not self.boundary:
            return None
        b, k, first, step = self.boundary
        if b < len(turns) and message_text(turns[b][0]) == first and (
                not k or (k + 1 < len(turns[b]) and message_text(turns[b][k + 1]) == step)):
            return b, k
        return None

    def _assemble(self, head, turns, bound):
        b, k = bound
        first = dict(turns[b][0])
        c = first.get("content")
        first["content"] = [{"type": "text", "text": self.layer}] + c if isinstance(c, list) else (
            self.layer + "\n" + (c or ""))
        return head + [first] + turns[b][1 + k:] + [m for t in turns[b + 1:] for m in t]

    def _fold(self, head, turns, tokens):
        """Fold for a target of tokens: the request becomes fold_to of it by the model's count (so the estimate aims
        at fold_to over the gain of the fold before, at most fold_cap), the exact tail and the layer by their shares.
        fold_to and fold_cap are the model's dials (_apply; FOLD_TO and FOLD_CAP by default). The gain is measured
        again here: the count of the last fold's request over the estimate that the calibration of now gives its
        aim. Between folds the calibration also learns that the layer is cheap, and a gain from the calibration of
        that fold counted the same thing twice: the second fold landed 10% over its aim (LandingTest)."""
        if self._landed:
            count, nbytes = self._landed
            self.gain = min(GAIN_RANGE[1], max(GAIN_RANGE[0], count / max(1.0, self.tokens(nbytes))))
        aim = min(self.fold_cap, self.fold_to / self.gain) * tokens
        target = self._bytes_for(aim)
        tail_share, layer_share = SMALL_SHARES if tokens <= SMALL_TARGET else (TAIL_SHARE, LAYER_SHARE)
        bound = cut(turns, tail_share * target)
        if bound is None:
            return None
        b, k = bound
        tail = request_bytes([turns[b][0]] + turns[b][1 + k:] + [m for t in turns[b + 1:] for m in t])
        entries = self._entries(turns, b, k)
        room = min(layer_share * target, target - request_bytes(head) - tail - len(LAYER_HEAD + LAYER_NOTE) - 64)
        levels = allocate(entries, int(room))
        self.layer = self._render(entries, levels)
        self.levels = {e["id"]: levels[e["id"]] for e in entries}
        self.layer_crumbs = sum(1 for v in levels.values() if v is not None)
        self.boundary = (b, k, message_text(turns[b][0]), message_text(turns[b][k + 1]) if k else "")
        self.folds += 1
        self._calls, self._fold_est, self._fold_bytes = calls_of(turns), aim, target
        self.store.flush()
        return bound

    def _tail_focus(self, focus, df, turns, b, k):
        """ace_persist: the words of the tool calls of the kept turns (the names and the arguments) are focus words at
        half weight, when a crumb has them (df: in how many crumbs each word is). A word that is focus already keeps
        its weight."""
        for turn in [turns[b][1 + k:]] + turns[b + 1:]:
            for m in turn:
                for name, args in _calls_of(m):
                    for w in _words(call_text(name, args)):
                        if df[w]:
                            focus.setdefault(w, 0.5)

    def _groups_of_words(self, seen, turns, b, k):
        """word -> the groups of GROUP_TURNS turns that use it: the folded crumbs (at their newest turn) and the kept
        turns (turns[b] after its first k steps, and the turns after it). ace_persist reads it."""
        used = collections.defaultdict(set)
        for e in seen.values():
            for w in e["words"]:
                used[w].add(e["turn"] // GROUP_TURNS)
        for t in range(b, len(turns)):
            for m in (turns[t][:1] + turns[t][1 + k:]) if t == b else turns[t]:
                for w in _words(crumb_text(m)):
                    used[w].add(t // GROUP_TURNS)
        return used

    def _entries(self, turns, b, k=0):
        """One entry per distinct message text that folds (turns[:b] and turns[b][1:1 + k]), with its weight
        and its floor."""
        calls, floors = set(), {}
        for turn in turns:
            for m in turn:
                for call in m.get("tool_calls") or []:
                    f = (call.get("function") or {}) if isinstance(call, dict) else {}
                    if f.get("name") not in CRUMB_TOOLS:
                        continue
                    calls.add(call.get("id"))
                    try:
                        args = json.loads(f.get("arguments") or "{}")
                        ref, level = str(args.get("id", "")), float(args.get("level", 0.25))
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if ref:
                        floors[ref] = max(floors.get(ref, 0.0), min(1.0, max(LEVELS[1], level)))
        new = _words(message_text(turns[-1][0]))  # the new message leads; the one before counts half
        focus = dict.fromkeys(_words(message_text(turns[-2][0])) if len(turns) > 1 else (), 0.5)
        focus.update(dict.fromkeys(new, 1.0))
        last_seen = {}  # anchor -> the last turn that mentions it
        for t, turn in enumerate(turns):
            for a in {x for m in turn for x in _anchors(crumb_text(m))}:
                last_seen[a] = t
        seen = {}
        folded = list(enumerate(turns[:b])) + ([(b, turns[b][1:1 + k])] if k else [])
        for t, turn in folded:
            for m in turn:
                if m.get("role") == "tool" and m.get("tool_call_id") in calls:
                    continue  # a view of a crumb: the crumb itself carries it, through its floor
                text = crumb_text(m)
                if not text.strip():
                    continue
                ref = ref_of(self.store, text)
                if ref in seen:
                    e = seen.pop(ref)  # the same text again: one crumb, at its newest place
                    e["count"] += 1
                else:
                    content = m.get("content")
                    said = content.strip() if isinstance(content, str) else content
                    role = "call" if m.get("tool_calls") and not said else str(m.get("role", "?"))
                    e = {"id": ref, "role": role, "size": len(text.encode("utf-8")), "count": 1, "text": text}
                e["turn"] = t
                seen[ref] = e
        for e in seen.values():
            e["words"] = _words(e["text"])
        df = collections.Counter(w for e in seen.values() for w in e["words"])
        if self.persist:
            self._tail_focus(focus, df, turns, b, k)
        idf = {w: f * math.log((len(seen) + 1) / (df[w] + 0.5)) for w, f in focus.items()}  # rare words count
        whole = sum(idf.values()) or 1.0
        used = self._groups_of_words(seen, turns, b, k) if self.persist else {}
        entries = []
        for e in seen.values():
            t, text = e.pop("turn"), e.pop("text")
            refs = sum(1 for a in _anchors(text) if last_seen.get(a, -1) > t)
            overlap = sum(idf[w] for w in e["words"] if w in idf) / whole
            floor = floors.get(e["id"], 0.0)
            weight = (ROLE_WEIGHT.get(e["role"], 1.0) * 0.5 ** (max(0, b - 1 - t) / HALF) * (1 + 8 * overlap)
                      * (1 + 0.5 * min(4, refs)) * (3 if floor else 1))
            if self.persist:  # a term that stays from the early turns: rare words that come back in later groups
                back = {g for w in e["words"] if math.log((len(seen) + 1) / (df[w] + 0.5)) >= RARE_IDF
                        for g in used.get(w, ()) if g > t // GROUP_TURNS}
                weight *= 1 + self.persist * min(4, len(back))
            entries.append(dict(e, group=t // GROUP_TURNS, turn=t, weight=weight, floor=floor, hot=overlap >= 0.3))
        entries.sort(key=lambda e: e["turn"])
        by = tags.successors(self._index, [e["id"] for e in entries])
        for e in entries:
            if e["id"] in by:  # superseded (tags.py): a handle that names its successor, never its text
                e.update(by=by[e["id"]], floor=0.0, hot=False)
        return entries

    def _render(self, entries, levels):
        out = [LAYER_HEAD, LAYER_NOTE]
        groups = collections.defaultdict(list)
        for e in entries:
            groups[e["group"]].append(e)
        for g in sorted(groups):
            es = groups[g]
            span = "turns %d-%d" % (es[0]["turn"] + 1, es[-1]["turn"] + 1)
            counts = collections.Counter(w for e in es for w in e["words"])
            cues = " ".join(sorted(counts, key=lambda w: (-counts[w], w))[:6])  # a tie goes by the word, not by chance
            if levels[es[0]["id"]] is None:  # the whole group is one crumb: its messages, each under its ref
                joined = "".join("%s %s %s\n%s\n" % (GAP, e["id"], e["role"], self.zoom.render(e["id"], 1.0)) for e in es)
                out.append("%s %s group \u00b7%dt | %s | %s\n" % (HANDLE, ref_of(self.store, joined),
                                                                  len(joined.encode("utf-8")) // 4, span, cues))
                continue
            out.append("\u2500 %s | %s\n" % (span, cues))
            for e in es:
                level = levels[e["id"]]
                out.append("%s %s %s \u00b7%dt%s%s%s\n" % (HANDLE, e["id"], e["role"], (e["size"] + 3) // 4,
                                                          " \u00d7%d" % e["count"] if e["count"] > 1 else "",
                                                          " \u25c6" if e["hot"] else "",
                                                          " superseded by " + e["by"] if e.get("by") else ""))
                if level and not e.get("by"):
                    body = self.zoom.render(e["id"], level).rstrip("\n")
                    out.append("".join("  " + line + "\n" for line in body.split("\n")))
        return "".join(out)
