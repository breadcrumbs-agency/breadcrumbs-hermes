"""Hermes's own full-text index of a fixture state.db, as Hermes keeps it (hermes_state: messages_fts over the view
messages_fts_src, filled by a trigger on each insert): breadcrumbs searches the texts of state.db there
(hermes_ingest.find) and keeps no words of them. Call messages_fts(db) after CREATE TABLE messages."""


def messages_fts(db):
    cols = [c[1] for c in db.execute("PRAGMA table_info(messages)")]
    calls = "tool_calls" if "tool_calls" in cols else "NULL"
    name = "tool_name" if "tool_name" in cols else "NULL"
    db.execute("CREATE VIEW messages_fts_src AS SELECT id, CASE WHEN role = 'tool' THEN substr(COALESCE(content, ''), 1, "
               "8192) ELSE content END AS content, %s AS tool_name, %s AS tool_calls FROM messages" % (name, calls))
    db.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls, content='messages_fts_src', "
               "content_rowid='id')")
    db.execute("CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN INSERT INTO messages_fts(rowid, content, "
               "tool_name, tool_calls) VALUES (new.id, CASE WHEN new.role = 'tool' THEN substr(COALESCE(new.content, ''), "
               "1, 8192) ELSE new.content END, %s, %s); END" % ("new." + name if name != "NULL" else "NULL",
                                                               "new." + calls if calls != "NULL" else "NULL"))
