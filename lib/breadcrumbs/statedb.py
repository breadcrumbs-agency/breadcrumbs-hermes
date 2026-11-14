"""statedb: the numbers of each session of a store, in the shape of Hermes's state.db, for the viewers.

A harness that keeps no database (Claude Code, Codex, OpenCode: files of sessions) gets <store>/state.db from `sync`
(sync.py). The viewers of breadcrumbs-spacegraph draw the time, the tokens, the codebase and the branch of each session
from such a file (view.py SESSION_COLS, MODEL_USAGE_COLS), and the harness gives them the reader as the port "state".
The file holds the columns that the views read and nothing of a message:

    sessions            id, title, created_source, parent_session_id, started_at, ended_at, last_activity_at (seconds),
                        message_count, tool_call_count, api_call_count, input_tokens, output_tokens, cache_read_tokens,
                        cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd (both NULL: the cost
                        of a request is not known here), cwd, git_repo_root (the root of the scope), git_branch
    session_model_usage session_id, model, task, estimated_cost_usd, actual_cost_usd, cost_status, cost_source,
                        first_seen, last_seen (one row for each session, model and task)

write() builds the file again, in a temporary file that replaces the old one, so a viewer that has it open reads a whole
file. locate() says which file the viewers read for a store: Hermes keeps its own state.db in the folder above the
store, and that one wins.
"""
import os
import sqlite3
import tempfile

NAME = "state.db"
SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, title TEXT, created_source TEXT, parent_session_id TEXT,
    started_at REAL, ended_at REAL, last_activity_at REAL,
    message_count INTEGER, tool_call_count INTEGER, api_call_count INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
    cwd TEXT, git_repo_root TEXT, git_branch TEXT);
CREATE TABLE session_model_usage (
    session_id TEXT, model TEXT, task TEXT, estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT,
    cost_source TEXT, first_seen REAL, last_seen REAL, PRIMARY KEY (session_id, model, task));
"""


def locate(home):
    """The state.db that the viewers read for the store at `home`: the one in the folder above it when there is one (the
    state.db of a Hermes profile), else the one inside it (from sync), else the path above it (nothing there). The
    store of a scope has its own state.db when sync gave it one; with none, it reads the state.db of its base."""
    from .scope import base_of  # a scope's store is under <base>/scopes: the state.db is beside the base

    home = os.path.abspath(os.path.expanduser(home))
    if base_of(home) != home and os.path.isfile(os.path.join(home, NAME)):
        return os.path.join(home, NAME)  # a scope that sync gave its own state.db
    home = base_of(home)
    beside, inside = os.path.join(os.path.dirname(home), NAME), os.path.join(home, NAME)
    return beside if os.path.isfile(beside) or not os.path.isfile(inside) else inside


def _insert(conn, table, row):
    conn.execute("INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (table, ", ".join(row), ", ".join("?" * len(row))),
                 list(row.values()))


def rows(source, root, sid, title, meta, session_rows, asked):
    """(sessions row, [session_model_usage rows]) of one session: its rows as the ingest takes them, and the usage records
    of its requests. `root` is the root of the scope that holds the session (or None)."""
    stamps = [r["ts"] for r in session_rows if r.get("ts")] + [r["ts"] for r in asked if r.get("ts")]
    t0, t1 = (min(stamps), max(stamps)) if stamps else (None, None)
    meta = meta or {}
    models = {}
    for r in asked:
        seen = models.setdefault((r.get("model") or "unknown", r.get("task") or "main"), [])
        seen += [r["ts"]] if r.get("ts") else []
    session = {"id": sid, "title": title, "created_source": source, "parent_session_id": meta.get("parent_session_id"),
               "started_at": t0, "ended_at": t1, "last_activity_at": t1, "message_count": len(session_rows),
               "tool_call_count": sum(len(r.get("tool_calls") or ()) for r in session_rows), "api_call_count": len(asked),
               "input_tokens": sum(r["in"] for r in asked), "output_tokens": sum(r["out"] for r in asked),
               "cache_read_tokens": sum(r["cr"] for r in asked), "cache_write_tokens": sum(r["cw"] for r in asked),
               "reasoning_tokens": sum(r.get("rsn") or 0 for r in asked), "cwd": meta.get("cwd"), "git_repo_root": root,
               "git_branch": meta.get("branch")}
    return session, [{"session_id": sid, "model": m, "task": t, "cost_status": "unknown", "cost_source": source,
                      "first_seen": min(s) if s else None, "last_seen": max(s) if s else None}
                     for (m, t), s in sorted(models.items())]


def write(home, source, sessions, records, root=None):
    """Build <home>/state.db from `sessions` ([(id, title, meta, rows)], as the ingest takes them) and `records` (the usage
    records of their requests, usage.record()). `source`: the key of the harness; `root`: the root of the scope of this
    store, or None. Returns the path of the file."""
    by = {}
    for r in records:
        by.setdefault(r.get("sess"), []).append(r)
    target = os.path.join(os.path.abspath(os.path.expanduser(home)), NAME)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=os.path.dirname(target))
    os.close(fd)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA)
        for sid, title, meta, session_rows in sessions:
            session, models = rows(source, root, sid, title, meta, session_rows, by.get(sid, ()))
            _insert(conn, "sessions", session)
            for m in models:
                _insert(conn, "session_model_usage", m)
        conn.commit()
    except BaseException:
        conn.close()
        os.unlink(tmp)
        raise
    conn.close()
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)
    return target


def read(path, fn, connect=None):
    """fn(conn) on a fresh read-only connection to the state.db at `path`, closed at once: the port "state"."""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    uri = "file:%s?mode=ro" % os.path.realpath(path).replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    conn = (connect or sqlite3.connect)(uri, uri=True, timeout=2.0, check_same_thread=False)
    try:
        conn.execute("PRAGMA query_only = 1")
        return fn(conn)
    finally:
        conn.close()
