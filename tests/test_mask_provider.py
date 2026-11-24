import _scope_off  # noqa: F401  (the provider uses the default memory in any folder)
import os
import tempfile
import unittest

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs.crumbs import get_crumb
from breadcrumbs_hermes.hermes import BreadcrumbsProvider

KEY = "sk-proj-Qm4Zr8TnV2xLc9HbYw3Ek7Ua"


class MaskProviderTest(unittest.TestCase):
    """A key that the user types reaches no blob, no title and no receipt (the plugin's write paths)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = BreadcrumbsProvider()
        self.p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")

    def tearDown(self):
        self.p.shutdown()
        self.tmp.cleanup()

    def blobs(self):
        self.p.store.flush()
        return b"\n".join(bytes(self.p.store.get(i)) for i in self.p.store.ids())

    def test_typed_text_query_and_delegation_are_masked(self):
        p = self.p
        p.sync_turn("remember my walrus kettle key %s please" % KEY, "saved", session_id="s1")
        p.on_session_switch("s2", reset=True)
        p.prefetch("walrus kettle key %s" % KEY, session_id="s2")
        p.on_delegation("check walrus %s" % KEY, "done with %s" % KEY, child_session_id="")
        rcpt = get_crumb(p.store, p.refs.get("rcpt-s2"))
        self.assertNotIn(KEY[-12:], rcpt.meta["q"])
        self.assertNotIn(KEY[-12:].encode(), self.blobs())
        self.assertNotIn(KEY[-12:], get_crumb(p.store, p.session_crumb("s1")).title)


if __name__ == "__main__":
    unittest.main()
