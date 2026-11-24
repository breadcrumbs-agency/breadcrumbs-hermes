"""Read a Hermes state.db (opened read-only) into a breadcrumbs store, without a copy of its text.

Each session becomes a session crumb (ref "sess-<id>") over its messages in order: user and assistant rows as "turn"
crumbs, tool rows as "tool" crumbs, each linked to the one before it (neighbor smoothing in search). A session row's
parent_session_id and source go into the session crumb's meta, so a child session names its parent. A database whose
sessions table lacks a column ingests as before, without that key.

state.db keeps the text; the store keeps the spine of each text and its locator "hermes:<message id>" (lines.put_text),
no line. source() gives the text back for a locator: the row as rows() reads it. The session crumb's meta "upto" is
the highest message id that it covers, so an import reads only the rows after it, and a session with no new row is
not read at all. The provider's sync_turn and session end, /viz, ingest-hermes and the family dial all import through
import_session: one writer, one rule.

state.db keeps typed text as typed: Hermes masks secrets only on the way to a memory provider
(agent/memory_manager.py _redact_for_provider). Every text read here goes through that same scrub (egress_scrub), the
spine is of the scrubbed text, and source() scrubs again on each read. Without Hermes, nothing is read.
"""

import collections
import inspect
import os
import sqlite3
import threading
import time

from breadcrumbs.crumbs import get_crumb
from breadcrumbs.history import Later, _ref_name, commit
from breadcrumbs.ingest import leaves, put_rows, put_tree, row_text
from breadcrumbs.runtime import no_gc
from breadcrumbs.statedb import locate

ROLES = ("user", "assistant", "tool")
SESSION_META = ("parent_session_id", "source")  # sessions columns that the session crumb keeps in its meta
OPTIONAL = ("tool_calls", "tool_call_id")  # messages columns that an older state.db may lack: read as NULL


def egress_scrub():
    """Hermes's scrub of what it hands a memory provider (agent.redact.redact_for_egress, as memory_manager._scrub
    calls it: urls=True where this Hermes has the flag). RuntimeError when Hermes is not importable."""
    try:
        from agent.redact import redact_for_egress
    except ImportError as e:
        raise RuntimeError("state.db is read only through Hermes's secret scrub (agent.redact): "
                           "run this with the Python of Hermes") from e
    if "urls" in inspect.signature(redact_for_egress).parameters:
        return lambda text: redact_for_egress(text, urls=True)
    return redact_for_egress


COLS = ("id", "role", "content", "tool_name", "timestamp", "tool_calls", "tool_call_id")


def _cols(con, optional=OPTIONAL):
    have = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    return ", ".join(c if c in have or c not in optional else "NULL" for c in COLS)


def _row(record, scrub, db=None):
    """One messages record as ingest.put_rows takes it, its texts through scrub, with its locator; None for a row
    that a store does not keep."""
    mid, role, content, tool_name, ts, calls, call_id = record
    if role not in ROLES or not (content or (role == "assistant" and calls)):
        return None
    text = content if isinstance(content, str) else ("" if content is None else str(content))
    return {"role": role, "content": scrub(text) if text else text, "tool_name": tool_name, "ts": int(ts or 0),
            "tool_calls": scrub(calls) if isinstance(calls, str) and calls else calls, "tool_call_id": call_id,
            "loc": "hermes:%d" % mid + ("@" + db if db else "")}


def rows(con, sid, roles=ROLES, optional=OPTIONAL, scrub=None, after=0, db=None):
    """The rows of session sid after message id `after` that a store keeps, in order, as ingest.put_rows takes them,
    each text through scrub (default egress_scrub()), each with its locator (db: the path that it names, when the
    store's own state.db is another; see source). A column of `optional` that messages lacks reads as NULL."""
    scrub = scrub or egress_scrub()
    out = []
    for record in con.execute("SELECT %s FROM messages WHERE session_id = ? AND id > ? ORDER BY id"
                              % _cols(con, optional), (sid, after)).fetchall():
        row = _row(record, scrub, db)
        if row is not None and row["role"] in roles:
            out.append(row)
    return out


def _db_name(db_path, store):
    """"" when db_path is the state.db of the store (statedb.locate), else its absolute path (for the locator)."""
    db = os.path.abspath(db_path)
    return "" if locate(os.path.dirname(store.path)) == db else db


def import_session(con, store, index, refs, sid, scrub, title="", meta=None, db=""):
    """Bring the session crumb of sid up to state.db: the rows after its meta "upto" become crumbs after its turns.
    Returns (session crumb, the new crumbs), or None when state.db has no row of sid. A session crumb without "upto"
    (one written before) is made again from all the rows. Reads no text of a session with no new row."""
    top = con.execute("SELECT MAX(id) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0]
    if top is None:
        return None
    cur = refs.get(_ref_name("sess-", sid))
    old = get_crumb(store, cur) if cur else None
    upto = old.meta.get("upto") if old else None
    if upto is not None and upto >= top:
        return cur, []
    kept = leaves(store, cur) if upto is not None else []
    new, _ = put_rows(store, index, rows(con, sid, scrub=scrub, after=upto or 0, db=db), sid,
                      prev=kept[-1] if kept else None)
    turns = kept + new
    if not turns:
        return None
    if not title and old and upto is not None:
        title = old.title
    m = dict(meta or (old.meta if old else {}), sess=sid, upto=top)
    sess = put_tree(store, "sess", title or get_crumb(store, turns[0]).title, turns, meta=m)
    refs.set(_ref_name("sess-", sid), sess)
    return sess, new


def session_import(db_path, store, index, refs, sid, title=""):
    """import_session of one session of the state.db at db_path (read-only); None when it has no row of sid."""
    scrub = egress_scrub()
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True)
    try:
        return import_session(con, store, index, refs, sid, scrub, title, db=_db_name(db_path, store))
    finally:
        con.close()


def sessions(db_path, roles=ROLES):
    """The source of the harness hermes (ports.harness, for `ingest` and `sync`): the sessions of the state.db at
    db_path (read-only), oldest first, each (id, title, meta, rows), every text through egress_scrub(), each row with
    a locator that names db_path. The database closes when the iterator ends."""
    scrub = egress_scrub()  # before state.db is opened: without Hermes, nothing is read
    db = os.path.abspath(db_path)
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        cols = ", ".join(c if c in have else "NULL" for c in SESSION_META)
        for sid, title, *kin in con.execute("SELECT id, COALESCE(title, ''), %s FROM sessions"
                                            " ORDER BY started_at, id" % cols).fetchall():
            meta = {k: str(v) for k, v in zip(SESSION_META, kin) if v}  # NULL and empty: no key
            yield sid, scrub(title) if title else title, meta, rows(con, sid, roles, scrub=scrub, db=db)
    finally:
        con.close()


def ingest_state_db(db_path, store, index, refs, roles=ROLES):
    """Import every session of the state.db at db_path (read-only), oldest first: one commit on "main" with the
    session crumbs that changed. Returns {sessions, messages, seconds}: the sessions that changed, their new crumbs."""
    t0 = time.perf_counter()
    scrub = egress_scrub()  # before state.db is opened: without Hermes, nothing is read
    db = _db_name(db_path, store)
    stats, roots = {"sessions": 0, "messages": 0}, []
    later = Later(refs)  # the refs wait for the store: no ref names a crumb that a crash can lose
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        cols = ", ".join(c if c in have else "NULL" for c in SESSION_META)
        with no_gc():
            for sid, title, *kin in con.execute("SELECT id, COALESCE(title, ''), %s FROM sessions"
                                                " ORDER BY started_at, id" % cols).fetchall():
                meta = {k: str(v) for k, v in zip(SESSION_META, kin) if v}  # NULL and empty: no key
                got = import_session(con, store, index, later, sid, scrub, scrub(title) if title else "", meta, db)
                if got and got[1]:
                    roots.append(got[0])
                    stats["sessions"] += 1
                    stats["messages"] += len(got[1])
    finally:
        con.close()
    if roots:
        commit(store, refs, "main", roots, "ingest " + os.path.basename(db_path))
    store.flush()
    later.flush()
    index.commit()
    stats["seconds"] = round(time.perf_counter() - t0, 3)
    return stats


_TEXTS = collections.OrderedDict()  # (db, message id) -> the bytes of its row: the texts that a render reads again
_TEXTS_LOCK = threading.Lock()


def source(store, loc):
    """The port "source:hermes": (the text of the locator "hermes:<message id>[@<state.db>]" as state.db keeps it, as
    Hermes's scrub gives it (the form that the import reads), the scrub), or None when the row is gone or Hermes is
    absent. lines.doc_lines checks either form against the spine and shows the masked one."""
    place, _, db = loc.partition(":")[2].partition("@")
    db = db or locate(os.path.dirname(store.path))
    key = (db, place)
    try:
        scrub = egress_scrub()
    except RuntimeError:
        return None
    with _TEXTS_LOCK:
        if key in _TEXTS:
            _TEXTS.move_to_end(key)
            return _TEXTS[key]
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            record = con.execute("SELECT %s FROM messages WHERE id = ?" % _cols(con), (int(place),)).fetchone()
        finally:
            con.close()
    except (sqlite3.Error, ValueError):
        return None
    row = _row(record, lambda t: t) if record else None
    got = None
    if row is not None:
        got = (row_text(row).encode("utf-8"), row_text(_row(record, scrub)).encode("utf-8"), scrub)
    with _TEXTS_LOCK:
        _TEXTS[key] = got
        if len(_TEXTS) > 512:
            _TEXTS.popitem(last=False)
    return got


def relocate(store_paths, db_path):
    """Once for each store (the file "relocated" in it says it ran): every text of it that a row of the state.db at
    db_path holds, as it is or as Hermes's scrub gives it, gets that row's locator, and its copy is dropped
    (lines.relocate). state.db is read once for all the stores, and a row is scrubbed at most once. Returns
    {store path: blobs dropped} of the stores that ran; {} when state.db or Hermes is absent."""
    from breadcrumbs.lines import relocate as relocate_store
    todo = [p for p in dict.fromkeys(store_paths)
            if os.path.isdir(p) and not os.path.exists(os.path.join(p, "relocated"))]
    if not todo or not os.path.exists(db_path):
        return {}
    try:
        scrub = egress_scrub()
    except RuntimeError:
        return {}
    db = _db_name(db_path, type("S", (), {"path": todo[0]})())
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True)
    try:
        records = con.execute("SELECT %s FROM messages ORDER BY id" % _cols(con)).fetchall()
    finally:
        con.close()
    rows, masked = [], {}
    for record in records:
        row = _row(record, lambda t: t, db)
        if row is not None:
            rows.append((row["loc"], row_text(row).encode("utf-8"), record))

    def scrubbed(loc, record):
        if loc not in masked:
            masked[loc] = row_text(_row(record, scrub, db)).encode("utf-8")
        return masked[loc]

    out = {}
    for path in todo:
        out[path] = relocate_store(path, ((loc, (raw, lambda l=loc, r=record: scrubbed(l, r)))
                                          for loc, raw, record in rows))
        with open(os.path.join(path, "relocated"), "w", encoding="utf-8") as f:
            f.write("%d\n" % out[path])
    return out
