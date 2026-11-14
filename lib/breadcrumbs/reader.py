"""The reader of a store: the read-only access that the verbs of common need (bc, find, open, zoom, trail, the vault
export, the hive). The viewers (view.py) build on it. A Reader creates and changes no file.

This module is in the layer common. find reads the dial "neighbor" of the store (dials.py).
"""
import json
import os
import threading

from . import dials
from .crumbs import Crumb, is_crumb
from .history import Refs, _ref_name
from .index import Index
from .store import Store
from .zoom import Zoom

STACK_MAX = 8  # SpaceGraph's Zoomed.ZOOM_STACK_MAX


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



class Reader:
    """Read-only access to one breadcrumbs folder (the provider's root)."""

    def __init__(self, root, hide=None):
        """Open a store to read. A Reader creates and changes no file: a folder without a store raises
        FileNotFoundError. close() releases the files. hide: for the views that draw (the explorer, term, spacegraph,
        the desktop page), novis.Hide or any hide(sid, prefix=False) -> bool: a session that it hides is left out of
        every answer and refused by id, and so are its crumbs. None (bc, vault, core, hive): every session."""
        if not os.path.isfile(os.path.join(root, "index.db")):
            raise FileNotFoundError(os.path.join(root, "index.db"))
        self.root, self.hide, self._closed = root, hide, False
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

    def crumb(self, ref, level):
        with self.lock:
            if self._hidden_ref(ref):
                raise KeyError(ref)
            return {"id": ref, "level": level, "tokens": self.zoom.full(ref), "text": self.zoom.render(ref, level)}

    def find(self, q, k=8):
        """Search hits grouped by session: [{session, hits: [{id, ref, score}]}], best first."""
        nb = dials.value(dials.config(self.root), None, "neighbor")
        with self.lock:
            groups, n = {}, 0
            for cid, ref, score in self.index.search(q, 3 * k, nb=nb):
                blob = self.store.get(cid)
                sid = Crumb.decode(blob).meta.get("sess", "") if is_crumb(blob) else ""
                if self._hidden(sid):
                    continue
                sess = self.refs.get(_ref_name("sess-", sid)) if sid else None
                groups.setdefault(sess or cid, {"session": sess, "hits": []})["hits"].append(
                    {"id": cid, "ref": ref, "score": round(score, 3)})
                n += 1
                if n >= k:
                    break
            return list(groups.values())

    def _crumb(self, ref):
        blob = self.store.get(ref.partition(":")[0])
        return Crumb.decode(blob) if ":" not in ref and is_crumb(blob) else None

    def _sid(self, ref, c):
        """The session id of a crumb, or of the first indexed crumb that holds a text ("" when not known)."""
        if c is None:
            holder = self.index.holder(ref.partition(":")[0])
            c = self._crumb(holder) if holder else None
        return c.meta.get("sess", "") if c else ""

    def _hidden(self, sid, prefix=False):
        """True when self.hide hides session sid ("": a crumb of no session, hidden when the whole profile is)."""
        return self.hide is not None and bool(self.hide(sid or "", prefix))

    def _hidden_ref(self, ref):
        """True when ref (a crumb id, or a text "doc:a-b") is of a session that self.hide hides."""
        if self.hide is None:
            return False
        try:
            return self._hidden(self._sid(ref, self._crumb(ref)))
        except (KeyError, ValueError, OSError):
            return self._hidden("")

    def close(self):
        """Release the store and index files (a second call does nothing)."""
        with self.lock:
            if not self._closed:
                self._closed = True
                self.store.close()
                self.index.close()

    def trail(self):
        """The zoom stack of the chat in progress (trail.json). With hide: none for a chat that it hides, and no crumb of
        a session that it hides."""
        try:
            with open(os.path.join(self.root, "trail.json"), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return Trail().to_dict()
        if self.hide is None or not isinstance(d, dict):
            return d
        with self.lock:
            if self._hidden(d.get("session", "")):
                return Trail().to_dict()
            stack = [e for e in d.get("stack", []) if not self._hidden_ref(str(e.get("id", "")))]
        return d if len(stack) == len(d.get("stack", [])) else Trail(stack, d.get("dropped", 0), d.get("session", "")).to_dict()
