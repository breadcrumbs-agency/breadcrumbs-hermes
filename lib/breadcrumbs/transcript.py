"""The parts that the session sources of the harnesses share (claude, codex, opencode: their sessions.py).

A source reads the stored sessions of a program and gives (id, title, meta, rows) to ingest.ingest_sessions. This module
holds what two or more sources need so that no source repeats it: the files of a program, a JSON-lines reader that skips
a broken line, the time stamp, the text of message parts, the meta of a session, and Rows, which builds the rows.
"""
import datetime
import glob
import json
import os


def files(path, default, *patterns):
    """The files of a program that ingest reads: `path` itself when it is a file; else the files that match the
    `patterns` under it (default: the folder where the program keeps them), in name order."""
    path = os.path.expanduser(path or default)
    if os.path.isfile(path):
        return [path]
    return sorted({f for pattern in patterns for f in glob.glob(os.path.join(glob.escape(path), pattern))})


class JsonLines:
    """The records of a JSON-lines file, in order. A line that is not a JSON object is skipped; `bad` counts them
    (read it after the loop)."""

    def __init__(self, path):
        self.path, self.bad = path, 0

    def __iter__(self):
        with open(self.path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    record = None
                if isinstance(record, dict):
                    yield record
                else:
                    self.bad += 1


def epoch(stamp):
    """Seconds since 1970 of an ISO 8601 time stamp (a text such as 2026-11-11T21:06:32.123Z); 0 when there is none."""
    try:
        return int(datetime.datetime.fromisoformat(stamp).timestamp())
    except (TypeError, ValueError):
        return 0


def text_parts(parts):
    """The text of a message content: a text as it is; of a list of parts, the text of each part that has one."""
    if isinstance(parts, list):
        return "\n".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
    return parts if isinstance(parts, str) else ""


def meta(**values):
    """The meta of a session crumb: each value that is not empty, as text. The keys that the sources share: cwd (the
    working folder), branch (the git branch), parent_session_id, skipped (the count of records that were skipped)."""
    return {key: str(value) for key, value in values.items() if value}


class Rows:
    """The rows of one session, in the dict form of ingest.put_rows. Call say, call and result in the order of the
    file. The parts of one assistant message (its text, then its tool calls) become one row: a user text or a tool
    result closes it."""

    def __init__(self):
        self.rows, self._open, self._names = [], None, {}

    def say(self, role, text, ts=0):
        """A user or assistant text; an empty text adds nothing."""
        if not text:
            return
        if role == "assistant":
            row = self._assistant(ts)
            row["content"] = (row["content"] + "\n" + text) if row["content"] else text
        else:
            self._open = None
            self.rows.append({"role": role, "content": text, "ts": ts})

    def call(self, call_id, name, arguments, ts=0):
        """A tool call of the assistant (arguments: a dict, or a text)."""
        self._assistant(ts)["tool_calls"].append({"id": call_id or "", "function": {"name": name or "", "arguments": arguments or ""}})
        self._names[call_id] = name or ""

    def result(self, call_id, text, ts=0):
        """The result of the tool call `call_id`; an empty result adds nothing."""
        self._open = None
        if text:
            self.rows.append({"role": "tool", "content": text, "tool_name": self._names.get(call_id, ""),
                              "tool_call_id": call_id or "", "ts": ts})

    def _assistant(self, ts):
        if self._open is None:
            self._open = {"role": "assistant", "content": "", "ts": ts, "tool_calls": []}
            self.rows.append(self._open)
        return self._open
