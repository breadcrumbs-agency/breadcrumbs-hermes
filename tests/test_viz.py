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


class AutostartHook(unittest.TestCase):
    """The session-start hook of the Hermes part: a chat in a terminal opens the explorer once per process, of the store
    that /viz would open. The terminals themselves (cmux, tmux, WezTerm, Kitty) are tested in breadcrumbs-common."""

    def setUp(self):
        import tempfile

        from breadcrumbs_hermes import workdir
        self.workdir = workdir
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "profiles", "alpha")
        self.base = os.path.join(self.home, "breadcrumbs")
        os.makedirs(self.base)
        self.scope_store = os.path.join(self.base, "scopes", "app-1234abcd")
        patch = mock.patch.object(workdir, "_viz_started", False)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(workdir, "current_home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def hook(self):
        got = {}

        class Ctx:
            def register_command(self, name, fn, **kw):
                pass

            def register_hook(self, name, fn):
                got[name] = fn

        info = {"store": self.scope_store, "cwd": "/w/app", "root": "/w/app", "kind": "git", "on": True}
        self.workdir.register(Ctx(), mock.Mock(scope_info=info, spec=["scope_info"]))
        return got["on_session_start"]

    def start(self, platform="cli", session_id="s1"):
        """Call the hook; the thread it starts is waited for. The autostart calls are the result."""
        calls = []
        threads = []
        real = self.workdir.start_viz

        def watch(store, harness=self.workdir.HARNESS):
            t = real(store, harness)
            threads.append(t)
            return t

        with mock.patch.object(viz, "autostart", lambda home, **kw: calls.append((home, kw))), \
                mock.patch.object(self.workdir, "start_viz", watch):
            self.hook()(session_id=session_id, platform=platform)
            for t in threads:
                if t:
                    t.join(5)
        return calls

    def test_the_hook_is_registered_and_a_hermes_without_hooks_still_registers_the_commands(self):
        self.assertTrue(callable(self.hook()))
        cmds = {}

        class NoHooks:
            def register_command(self, name, fn, **kw):
                cmds[name] = fn

        self.workdir.register(NoHooks(), None)
        self.assertEqual(sorted(cmds), ["scope", "viz"])

    def test_a_terminal_chat_opens_the_explorer_of_the_whole_profile(self):
        for platform in ("cli", "tui"):
            with self.subTest(platform=platform), mock.patch.object(self.workdir, "_viz_started", False):
                self.assertEqual(self.start(platform), [(self.base, {"harness": "hermes"})])

    def test_once_per_process(self):
        self.assertEqual(len(self.start()), 1)
        self.assertEqual(self.start(session_id="s2"), [])

    def test_only_a_chat_in_a_terminal_starts_it(self):
        for platform in ("telegram", "discord", "desktop", "acp", "local", "", None):
            self.assertEqual(self.start(platform), [], platform)
        self.assertFalse(self.workdir._viz_started)  # a gateway chat first does not use up the one start

    def test_scope_narrows_it_as_it_narrows_slash_viz(self):
        import json
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump({"viz": "scope"}, f)
        self.assertEqual(self.start(), [(self.scope_store, {"harness": "hermes"})])

    def test_a_no_vis_profile_opens_nothing(self):
        with mock.patch.object(self.workdir.novis, "blocked", return_value="profile `alpha` is tagged no-vis"):
            self.assertEqual(self.start(), [])
        self.assertFalse(self.workdir._viz_started)

    def test_a_failure_never_stops_the_chat(self):
        with mock.patch.object(viz, "autostart", side_effect=LookupError("the explorer is not installed")):
            thread = self.workdir.start_viz(self.base)
            thread.join(5)
        self.assertFalse(thread.is_alive())

    @unittest.skipUnless(__import__("shutil").which("tmux"), "tmux is not installed")
    def test_in_a_real_tmux_the_chat_gets_a_second_pane(self):
        """Hermes path, real split: a private tmux server, the hook, and viz.autostart unmocked. The pane runs a stand-in for
        `term` (a sleep), because term needs a terminal that reads the Kitty graphics protocol."""
        import subprocess

        sock = "bchook-%d" % os.getpid()

        def tmux(*argv):
            return subprocess.run(["tmux", "-L", sock] + list(argv), capture_output=True, text=True, timeout=20)

        if tmux("new-session", "-d", "-x", "200", "-y", "50", "-s", "t", "sleep 60").returncode != 0:
            self.skipTest("tmux cannot start a server here")
        self.addCleanup(tmux, "kill-server")
        info = tmux("display-message", "-p", "-t", "t", "#{socket_path},#{pid},0|#{pane_id}").stdout.strip()
        tmux_env, pane = info.split("|")
        env = {"TMUX": tmux_env, "TMUX_PANE": pane, "PATH": os.environ.get("PATH", "")}
        threads = []
        real = self.workdir.start_viz
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(viz, "_term_argv", return_value=lambda home, harness=None: ["sleep", "61"]), \
                mock.patch.object(self.workdir, "start_viz", lambda *a, **k: threads.append(real(*a, **k)) or threads[-1]):
            self.hook()(session_id="s1", platform="cli")
            threads[0].join(10)
        panes = [p for p in tmux("list-panes", "-t", "t", "-F", "#{pane_id} #{pane_current_command}").stdout.split("\n") if p]
        self.assertEqual(len(panes), 2, panes)
        self.assertEqual(tmux("display-message", "-p", "-t", "t", "#{pane_id}").stdout.strip(), pane)  # the chat keeps the focus
