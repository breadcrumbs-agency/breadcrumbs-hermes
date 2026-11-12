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


if __name__ == "__main__":
    unittest.main()
