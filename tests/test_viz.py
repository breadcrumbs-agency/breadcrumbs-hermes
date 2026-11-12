"""`viz` (the explorer opens where the user is) and `help`."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli
from breadcrumbs_spacegraph import viz


class Viz(unittest.TestCase):
    def run_viz(self, which, run_rc):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=run_rc, stderr=b"denied")

        err = io.StringIO()
        with mock.patch.object(viz, "ensure_server", return_value="http://127.0.0.1:1/#k=x"), \
                mock.patch.object(viz.shutil, "which", return_value=which), \
                mock.patch.object(viz.subprocess, "run", fake_run), \
                mock.patch.object(viz.webbrowser, "open") as browser:
            rc = viz.run("home", err=err)
        return rc, calls, browser, err.getvalue()

    def test_cmux_gets_a_browser_split(self):
        rc, calls, browser, _ = self.run_viz("/x/cmux", 0)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [["/x/cmux", "browser", "open", "http://127.0.0.1:1/#k=x"]])
        browser.assert_not_called()

    def test_no_cmux_falls_back_to_the_browser(self):
        rc, calls, browser, _ = self.run_viz(None, 0)
        self.assertEqual((rc, calls), (0, []))
        browser.assert_called_once_with("http://127.0.0.1:1/#k=x")

    def test_a_refused_cmux_says_so_and_falls_back(self):
        rc, _, browser, err = self.run_viz("/x/cmux", 1)
        self.assertEqual(rc, 0)
        self.assertIn("cmux could not open it: denied", err)
        browser.assert_called_once()


class Autostart(unittest.TestCase):
    """viz.autostart: a split in cmux or tmux, the OS in a bare console. No real window opens here."""

    def start(self, env, which=lambda n: "/x/" + n, rc=0):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=rc, stderr=b"denied")

        with mock.patch.object(viz, "ensure_server", return_value="http://127.0.0.1:1/#k=x"), \
                mock.patch.object(viz.shutil, "which", which), \
                mock.patch.object(viz.subprocess, "run", fake_run), \
                mock.patch.object(viz, "can_launch", return_value=True), \
                mock.patch.object(viz.webbrowser, "open", return_value=True) as browser:
            where = viz.autostart("/h/breadcrumbs", env=env, err=io.StringIO())
        return where, calls, browser

    def test_cmux_gets_a_browser_split(self):
        where, calls, browser = self.start({"CMUX_SURFACE_ID": "s"})
        self.assertEqual((where, calls), ("cmux", [["cmux", "browser", "open", "http://127.0.0.1:1/#k=x"]]))
        browser.assert_not_called()

    def test_tmux_gets_a_pane_that_runs_term(self):
        where, calls, browser = self.start({"TMUX": "t", "TMUX_PANE": "%3"})
        self.assertEqual(where, "tmux")
        self.assertEqual(calls[0][:6], ["tmux", "split-window", "-h", "-d", "-t", "%3"])
        self.assertTrue(calls[0][-1].endswith("--home /h/breadcrumbs term"))
        browser.assert_not_called()

    def test_a_bare_console_asks_the_os(self):
        where, calls, browser = self.start({})
        self.assertEqual((where, calls), ("os", []))
        browser.assert_called_once_with("http://127.0.0.1:1/#k=x")

    def test_a_refused_split_falls_back_to_the_os(self):
        where, _, browser = self.start({"CMUX_SURFACE_ID": "s"}, rc=1)
        self.assertEqual(where, "os")
        browser.assert_called_once()

    def test_the_switch_turns_it_off(self):
        where, calls, browser = self.start({"CMUX_SURFACE_ID": "s", "BREADCRUMBS_VIZ": "0"})
        self.assertEqual((where, calls), (None, []))
        browser.assert_not_called()

    def test_linux_without_a_display_does_not_launch(self):
        self.assertFalse(viz.can_launch({}, platform="linux"))
        self.assertTrue(viz.can_launch({"DISPLAY": ":0"}, platform="linux"))


class Server(unittest.TestCase):
    """ensure_server: the port of another home's server is not taken for this home's."""

    def url(self, answers, listening):
        started = []
        with mock.patch("breadcrumbs_spacegraph.launch.answers", return_value=answers), \
                mock.patch("breadcrumbs_spacegraph.view.token", return_value="x"), \
                mock.patch.object(viz, "listening", side_effect=listening), \
                mock.patch.object(viz, "free_port", return_value=4321), \
                mock.patch.object(viz.subprocess, "Popen", lambda argv, **kw: started.append(argv)):
            return viz.ensure_server("home"), started

    def test_a_server_of_this_home_is_used_again(self):
        self.assertEqual(self.url(True, [AssertionError]), ("http://127.0.0.1:8765/#k=x", []))

    def test_a_port_that_another_server_holds_gives_a_free_port(self):
        url, started = self.url(False, [True, True])
        self.assertEqual(url, "http://127.0.0.1:4321/#k=x")
        self.assertEqual(started[0][-4:], ["home", "serve", "--port", "4321"])

    def test_a_free_port_gets_the_server(self):
        url, started = self.url(False, [False, True])
        self.assertEqual(url, "http://127.0.0.1:8765/#k=x")
        self.assertEqual(started[0][-2:], ["--port", "8765"])


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

        def fake_run(home, port=viz.PORT, err=None, terminal=True):
            seen.update(home=home, terminal=terminal)
            print("breadcrumbs viz: opened in the browser: http://127.0.0.1:1/#k=SECRET", file=err)
            return 0

        store = os.path.join("/h/profiles/alpha", "breadcrumbs", "scopes", "app-1234abcd")
        with mock.patch.object(viz, "run", fake_run), mock.patch.object(workdir, "current_home", return_value="/h/profiles/alpha"):
            out = workdir.open_viz(store, hermes="/h")
        self.assertEqual(seen, {"home": store, "terminal": False})  # the store of the chat's scope, not the profile's
        self.assertEqual(out, "breadcrumbs: the explorer of %s is open in the browser (read-only, on this machine)." % store)
        self.assertNotIn("SECRET", out)

    def test_a_failure_is_an_answer(self):
        from breadcrumbs_hermes import workdir
        with mock.patch.object(viz, "run", side_effect=OSError("no port")), \
                mock.patch.object(workdir, "current_home", return_value="/h/profiles/alpha"):
            self.assertIn("did not open: OSError: no port", workdir.open_viz("/h/profiles/alpha/breadcrumbs", hermes="/h"))


if __name__ == "__main__":
    unittest.main()
