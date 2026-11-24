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

import atexit
import collections
import inspect
import os
import re
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


_COLS_OF = {}  # db -> the columns that source() reads (NULL for one that an older state.db lacks)
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
        cols = _COLS_OF.get(db)
        if cols is None:
            have = {r[1] for r in _query(db, "PRAGMA table_info(messages)")}
            cols = _COLS_OF[db] = ", ".join(c if c in have or c not in OPTIONAL else "NULL" for c in COLS)
        record = _query(db, "SELECT %s FROM messages WHERE id = ?" % cols, (int(place),), one=True)
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


def stores(home):
    """The breadcrumbs folders of a Hermes profile home: its default memory and each scope's."""
    base = os.path.join(home, "breadcrumbs")
    out = [base]
    scopes = os.path.join(base, "scopes")
    if os.path.isdir(scopes):
        out += [os.path.join(scopes, n) for n in sorted(os.listdir(scopes)) if os.path.isdir(os.path.join(scopes, n, "store"))]
    return [r for r in out if os.path.isdir(os.path.join(r, "store"))]


def old_stores(home):
    """The breadcrumbs folders of the profile at home whose store a release before this one wrote (store.old)."""
    from breadcrumbs.store import old
    return [r for r in stores(home) if old(os.path.join(r, "store"))]


ADVICE = ("breadcrumbs: %d store(s) of this profile hold copies of state.db's texts (a release before 0.6.0). "
          "Convert them once, with Hermes stopped: python -m breadcrumbs convert-hermes %s")


def convert(roots, db_path):
    """Convert the old stores of the breadcrumbs folders roots (store.old): every text that a row of the state.db at
    db_path holds, as it is or as Hermes's scrub gives it, gets that row's locator and loses its copy (lines.relocate),
    and the index drops its words (Index.relocate). Every id and every crumb stay. state.db is read once, a row is
    scrubbed at most once. RuntimeError without Hermes's scrub; StoreBusy (breadcrumbs.store) when a store is open.
    Returns {root: (blobs dropped, index rows located)}."""
    from breadcrumbs.index import Index
    from breadcrumbs.lines import get_lines, relocate as relocate_store
    from breadcrumbs.store import Store, old
    todo = [r for r in dict.fromkeys(roots) if old(os.path.join(r, "store"))]
    if not todo:
        return {}
    scrub = egress_scrub()
    rows, masked = [], {}
    if os.path.exists(db_path):
        db = _db_name(db_path, type("S", (), {"path": os.path.join(todo[0], "store")})())
        con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True)
        try:
            records = con.execute("SELECT %s FROM messages ORDER BY id" % _cols(con)).fetchall()
        finally:
            con.close()
        for record in records:
            row = _row(record, lambda t: t, db)
            if row is not None:
                rows.append((row["loc"], row_text(row).encode("utf-8"), record))

    def scrubbed(loc, record):
        if loc not in masked:
            masked[loc] = row_text(_row(record, scrub, db)).encode("utf-8")
        return masked[loc]

    out = {}
    for root in todo:
        located, dropped = relocate_store(os.path.join(root, "store"),
                                          ((loc, (raw, lambda l=loc, r=record: scrubbed(l, r))) for loc, raw, record in rows))
        store = Store(os.path.join(root, "store"), readonly=True)
        index = Index(os.path.join(root, "index.db"))
        try:
            def text_of(ref):
                doc, _, span = ref.partition(":")
                a, _, b = span.partition("-")
                return b"".join(get_lines(store, doc, int(a), int(b))).decode("utf-8", "replace")
            out[root] = (dropped, index.relocate(located, text_of))
        finally:
            index.close()
            store.close()
    return out


_CONS = {}  # db -> one read-only connection for the process: a view reads thousands of rows (used under _CONS_LOCK)
_CONS_LOCK = threading.Lock()


def _query(db, sql, args=(), one=False):
    """The rows of sql on the read-only connection to db (one: the first row or None)."""
    with _CONS_LOCK:
        con = _CONS.get(db)
        if con is None:
            con = _CONS[db] = sqlite3.connect("file:%s?mode=ro" % db, uri=True, check_same_thread=False)
        cur = con.execute(sql, args)
        return cur.fetchone() if one else cur.fetchall()


def _close_all():
    with _CONS_LOCK:
        for con in _CONS.values():
            con.close()
        _CONS.clear()


atexit.register(_close_all)


def find(root, query, k):
    """The port "find:hermes": [(locator, -bm25)] of Hermes's own full-text index (messages_fts) of the state.db of the
    store at root, for an FTS5 query (Index._outside_hits: the porter stems of the words as prefixes, OR-ed). Up to
    5000 rows: the store of a scope holds only some sessions of state.db."""
    db = locate(root)
    if not os.path.isfile(db):
        return []
    try:
        got = _query(db, "SELECT rowid, bm25(messages_fts) FROM messages_fts WHERE messages_fts MATCH ? "
                         "ORDER BY bm25(messages_fts) LIMIT ?", (query, max(20 * k, 5000)))
    except sqlite3.Error:
        return []
    return [("hermes:%d" % rid, -s) for rid, s in got]


_VOCAB = {}  # db -> ((size, mtime), the answer of vocab)


def vocab(root, stems):
    """The port "vocab:hermes": (rows, tokens, terms, count(stem)) of Hermes's own index (messages_fts) of the state.db
    of the store at root, its words made into the stems of the breadcrumbs index by stems(words); read again when
    state.db changes size by more than 1%."""
    db = locate(root)
    try:
        size = os.path.getsize(db)
    except OSError:
        return None
    hit = _VOCAB.get(db)
    if hit is not None and abs(size - hit[0]) <= 0.01 * hit[0]:
        return hit[1]
    try:
        _query(db, "CREATE VIRTUAL TABLE IF NOT EXISTS temp.bc_vocab USING fts5vocab('main', 'messages_fts', 'row')")
        terms = _query(db, "SELECT term, doc, cnt FROM temp.bc_vocab")
        rows = _query(db, "SELECT count(*) FROM messages_fts_docsize", one=True)[0]
    except sqlite3.Error:
        return None
    counts = {}
    words = [t for t, _d, _c in terms]
    for i in range(0, len(words), 5000):
        for stem, j in stems(words[i:i + 5000]):
            _t, d, c = terms[i + j]
            got = counts.get(stem, (0, 0))
            counts[stem] = (got[0] + d, got[1] + c)
    answer = (rows, sum(c for _t, _d, c in terms), len(counts), lambda stem: counts.get(stem, (0, 0)))
    _VOCAB[db] = (size, answer)
    return answer
