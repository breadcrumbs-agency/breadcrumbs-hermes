"""`/viz` of Hermes (workdir.open_viz) and `help`. The explorer's own tests are in breadcrumbs-common (tests/test_viz.py)."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli, viz


class Help(unittest.TestCase):
    def test_help_is_a_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["help"])
        self.assertEqual(rc, 0)
        self.assertIn("viz", out.getvalue())


class VizCommand(unittest.TestCase):
    """/viz: the chat command opens the explorer of the chat's scope store and never starts the terminal viewer."""

    def test_the_answer_names_the_place_and_not_the_url(self):
        from breadcrumbs_hermes import workdir
        seen = {}

        def fake_run(home, port=viz.PORT, err=None, terminal=True, harness=None):
            seen.update(home=home, terminal=terminal, harness=harness)
            print("breadcrumbs viz: opened in the browser: http://127.0.0.1:1/#k=SECRET", file=err)
            return 0

        store = os.path.join("/h/profiles/alpha", "breadcrumbs", "scopes", "app-1234abcd")
        with mock.patch.object(viz, "run", fake_run), mock.patch.object(workdir, "current_home", return_value="/h/profiles/alpha"):
            out = workdir.open_viz(store, hermes="/h")
        self.assertEqual(seen, {"home": store, "terminal": False, "harness": "hermes"})  # the store of the chat's scope, not the profile's; and its icon
        self.assertEqual(out, "breadcrumbs: the explorer of %s is open in the browser (read-only, on this machine)." % store)
        self.assertNotIn("SECRET", out)

    def test_a_failure_is_an_answer(self):
        from breadcrumbs_hermes import workdir
        with mock.patch.object(viz, "run", side_effect=OSError("no port")), \
                mock.patch.object(workdir, "current_home", return_value="/h/profiles/alpha"):
            self.assertIn("did not open: OSError: no port", workdir.open_viz("/h/profiles/alpha/breadcrumbs", hermes="/h"))


class VizSeesTheWholeProfile(unittest.TestCase):
    """/viz opens the profile, not the folder: Hermes is not a secure portal and not a stable namespace, so the explorer sees
    every chat of state.db (imported first) and every scope; "viz": "scope" in config.json narrows it again."""

    def setUp(self):
        import json
        import tempfile

        from tests.test_hermes_ingest import make_db
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "profiles", "alpha")
        self.base = os.path.join(self.home, "breadcrumbs")
        self.scope_store = os.path.join(self.base, "scopes", "app-1234abcd")
        os.makedirs(self.scope_store)
        make_db(os.path.join(self.home, "state.db"))  # two chats that no store holds yet
        self.json = json

    def run_viz(self, provider=None):
        from breadcrumbs_hermes import workdir
        seen = []

        def fake_run(home, port=viz.PORT, err=None, terminal=True, harness=None):
            seen.append(home)
            return 0

        class Ctx:
            cmds = {}

            def register_command(self, name, fn, **kw):
                self.cmds[name] = fn

        ctx = Ctx()
        info = {"store": self.scope_store, "cwd": "/w/app", "root": "/w/app", "kind": "git", "on": True}
        with mock.patch.object(viz, "run", fake_run), mock.patch.object(workdir, "current_home", return_value=self.home):
            workdir.register(ctx, mock.Mock(scope_info=info, spec=["scope_info"]))
            out = ctx.cmds["viz"]("", "s1", "cli")
        return seen, out

    def sessions_in(self, store):
        from breadcrumbs.history import Refs
        return sorted(n for n in Refs(os.path.join(store, "refs")).names() if n.startswith("sess-"))

    def test_the_explorer_opens_the_base_after_importing_state_db(self):
        self.assertEqual(self.sessions_in(self.base), [])
        seen, out = self.run_viz()
        self.assertEqual(seen, [self.base])  # the profile, not the scope of the folder
        self.assertEqual(self.sessions_in(self.base), ["sess-s1", "sess-s2"])
        self.assertIn("is open", out)

    def test_the_import_is_idempotent(self):
        self.run_viz()
        self.run_viz()
        self.assertEqual(self.sessions_in(self.base), ["sess-s1", "sess-s2"])

    def test_scope_in_config_narrows_it_to_the_chats_scope(self):
        with open(os.path.join(self.base, "config.json"), "w", encoding="utf-8") as f:
            self.json.dump({"viz": "scope"}, f)
        seen, _ = self.run_viz()
        self.assertEqual(seen, [self.scope_store])

    def test_a_broken_state_db_still_opens_the_explorer_and_leaves_its_error(self):
        with open(os.path.join(self.home, "state.db"), "wb") as f:
            f.write(b"not a database")
        seen, out = self.run_viz()
        self.assertEqual(seen, [self.base])
        self.assertTrue(os.path.getsize(os.path.join(self.base, "ingest.err")) > 0)


if __name__ == "__main__":
    unittest.main()
