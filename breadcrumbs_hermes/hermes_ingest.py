"""Backfill: read a Hermes state.db (opened read-only) into a breadcrumbs store.

Each session becomes a session crumb (ref "sess-<id>") over its messages in
order: user and assistant rows as "turn" crumbs, tool rows as "tool" crumbs.
Each turn is linked to the one before it (neighbor smoothing in search).
Running it twice adds only a new commit: blobs and index rows dedupe.
A session row's parent_session_id and source go into the session crumb's meta, so a child session names
its parent. A database whose sessions table lacks a column ingests as before, without that key.

This module only reads state.db: sessions() is the source of the harness hermes (ports.harness), and the loop that
stores them is breadcrumbs/ingest.py ingest_sessions.

state.db keeps typed text as typed: Hermes masks secrets only on the way to a memory provider
(agent/memory_manager.py _redact_for_provider). Every text read here goes through that same scrub (egress_scrub),
before it is hashed, so a row enters the store as the hand-off of sync_turn brings it. Without Hermes, nothing is read.
"""

import inspect
import os
import sqlite3

from breadcrumbs.ingest import ingest_sessions

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


def rows(con, sid, roles=ROLES, optional=OPTIONAL, scrub=None):
    """The rows of session sid that a store keeps, in order, as ingest.put_rows takes them, each text through
    scrub (default egress_scrub()). A column of `optional` that messages lacks reads as NULL."""
    scrub = scrub or egress_scrub()
    have = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    cols = ", ".join(c if c in have or c not in optional else "NULL"
                     for c in ("role", "content", "tool_name", "timestamp", "tool_calls", "tool_call_id"))
    keep = []
    for role, content, tool_name, ts, calls, call_id in con.execute(
            "SELECT %s FROM messages WHERE session_id = ? ORDER BY id" % cols, (sid,)).fetchall():
        if role not in roles or not (content or (role == "assistant" and calls)):
            continue
        text = content if isinstance(content, str) else ("" if content is None else str(content))
        keep.append({"role": role, "content": scrub(text) if text else text, "tool_name": tool_name,
                     "ts": int(ts or 0), "tool_calls": scrub(calls) if isinstance(calls, str) and calls else calls,
                     "tool_call_id": call_id})
    return keep


def sessions(db_path, roles=ROLES):
    """The source of the harness hermes: the sessions of the state.db at db_path (read-only), oldest first, each
    (id, title, meta, rows), every text through egress_scrub(). The database closes when the iterator ends."""
    scrub = egress_scrub()  # before state.db is opened: without Hermes, nothing is read
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        cols = ", ".join(c if c in have else "NULL" for c in SESSION_META)
        for sid, title, *kin in con.execute("SELECT id, COALESCE(title, ''), %s FROM sessions"
                                            " ORDER BY started_at, id" % cols).fetchall():
            meta = {k: str(v) for k, v in zip(SESSION_META, kin) if v}  # NULL and empty: no key
            yield sid, scrub(title) if title else title, meta, rows(con, sid, roles, scrub=scrub)
    finally:
        con.close()


def ingest_state_db(db_path, store, index, refs, roles=ROLES):
    return ingest_sessions(store, index, refs, sessions(db_path, roles), "ingest " + os.path.basename(db_path))
