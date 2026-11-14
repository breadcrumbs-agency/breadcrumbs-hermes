"""The provider keeps one memory for each git repository or folder, not for each Hermes profile (workdir.py, scope.py)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import scope
from breadcrumbs_hermes import workdir
from breadcrumbs_hermes.hermes import BreadcrumbsProvider


class ScopedProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.realpath(self.tmp.name)
        self.hermes = os.path.join(self.dir, "hermes-home")  # the profile's home
        os.makedirs(self.hermes)
        self.user = os.path.join(self.dir, "user")  # a home folder for the scopes
        self.alpha = self.repo("work", "alpha")
        self.beta = self.repo("work", "beta")
        self.env = mock.patch.dict(os.environ, {"BREADCRUMBS_SCOPE": "auto", "HOME": self.user, "TERMINAL_CWD": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.home = mock.patch.object(scope, "HOME", self.user)  # scope.py reads its home when it is imported
        self.home.start()
        self.addCleanup(self.home.stop)
        self.providers = []

    def repo(self, *parts):
        path = os.path.join(self.user, *parts)
        os.makedirs(os.path.join(path, ".git"))
        return path

    def provider(self, cwd, session="s1", **kw):
        p = BreadcrumbsProvider()
        p.initialize(session, hermes_home=self.hermes, platform="cli", agent_context="primary", cwd=cwd, **kw)
        self.providers.append(p)
        self.addCleanup(p.shutdown)
        return p

    def find(self, p, q, **args):
        return json.loads(p.handle_tool_call("crumb_find", dict(q=q, **args)))["text"]

    def test_a_chat_in_a_repository_uses_that_repositorys_store_under_the_profile_home(self):
        p = self.provider(os.path.join(self.alpha, "src"))
        base = os.path.join(self.hermes, "breadcrumbs")
        self.assertEqual(p.root, scope.store_home(base, self.alpha))
        self.assertEqual(p.scope_info["kind"], "git")
        self.assertEqual(p.scope_info["root"], self.alpha)
        self.assertEqual(p._hermes_home(), self.hermes)  # the profile's home is not the store's parent
        self.assertTrue(os.path.isfile(os.path.join(p.root, "scope.json")))

    def test_a_folder_with_no_repository_is_its_own_project_and_the_home_folder_is_the_default_memory(self):
        base = os.path.join(self.hermes, "breadcrumbs")
        notes = os.path.join(self.user, "notes")
        os.makedirs(notes)
        p = self.provider(notes)
        self.assertEqual((p.scope_info["kind"], p.scope_info["root"]), ("dir", notes))
        self.assertEqual(p.root, scope.store_home(base, notes))
        home = self.provider(self.user, "s-home")
        self.assertEqual((home.scope_info["kind"], home.root), ("home", base))  # the default memory keeps its past

    def test_a_breadcrumbs_file_that_says_off_and_the_off_switch_use_the_default_memory(self):
        base = os.path.join(self.hermes, "breadcrumbs")
        with open(os.path.join(self.alpha, scope.MARKER), "w", encoding="utf-8") as f:
            f.write("off")
        self.assertEqual(self.provider(self.alpha).root, base)
        with mock.patch.dict(os.environ, {"BREADCRUMBS_SCOPE": "off"}):
            p = self.provider(self.beta, "s2")
        self.assertEqual(p.root, base)
        self.assertFalse(p.scope_info["on"])

    def test_the_cwd_comes_from_the_caller_then_hermes_then_terminal_cwd_then_the_process(self):
        self.assertEqual(workdir.chat_cwd({"cwd": "/a"}), "/a")
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": "/b"}):
            self.assertEqual(workdir.chat_cwd({}), "/b")
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": ""}):
            self.assertEqual(workdir.chat_cwd({}), os.getcwd())

    def test_recall_and_find_stay_in_the_scope_so_one_codebase_never_answers_for_another(self):
        a = self.provider(self.alpha, "sa")
        a.sync_turn("how do I tune the walrus kettle", "set the walrus kettle to eighty degrees", session_id="sa")
        b = self.provider(self.beta, "sb")
        b.sync_turn("what does the flamingo parser do", "the flamingo parser reads tokens", session_id="sb")
        self.assertIn("walrus", self.find(a, "walrus kettle"))
        self.assertNotIn("walrus", self.find(b, "walrus kettle"))  # beta does not see alpha
        self.assertEqual(b._recall("walrus kettle tuning", "sb"), "")
        self.assertIn("flamingo", self.find(b, "flamingo parser"))
        self.assertNotEqual(a.root, b.root)

    def test_scope_all_reads_the_other_scopes_and_up_reads_only_the_scopes_above(self):
        mono = self.repo("work", "mono")
        svc = os.path.join(mono, "svc")
        os.makedirs(svc)
        with open(os.path.join(svc, scope.MARKER), "w", encoding="utf-8") as f:
            f.write("")
        parent = self.provider(mono, "sm")
        parent.sync_turn("the monorepo uses the pelican build flag", "yes, pelican build flag is on", session_id="sm")
        child = self.provider(svc, "sc")
        child.sync_turn("the service logs go to the heron sink", "heron sink is configured", session_id="sc")
        other = self.provider(self.beta, "sb")
        other.sync_turn("beta stores the osprey cache", "osprey cache lives in beta", session_id="sb")
        self.assertEqual(child.scope_info["kind"], "explicit")
        self.assertEqual([r for _, r in child.scope_info["up"]], [mono])
        self.assertNotIn("pelican", self.find(child, "pelican build flag"))  # here: this scope only
        up = self.find(child, "pelican build flag", scope="up")
        self.assertIn("pelican", up)
        self.assertIn("\u25c7 scope", up)  # the block names its scope
        self.assertNotIn("osprey", self.find(child, "osprey cache", scope="up"))  # beta is not above
        self.assertIn("osprey", self.find(child, "osprey cache", scope="all"))

    def test_crumb_opens_an_id_that_another_scope_holds(self):
        a = self.provider(self.alpha, "sa")
        a.sync_turn("remember the tangerine ratio", "the tangerine ratio is three to one", session_id="sa")
        b = self.provider(self.beta, "sb")
        sess = a.session_crumb("sa")
        # b does not hold it: it answers from another scope, with the scope named
        got = json.loads(b.handle_tool_call("crumb", {"id": sess, "level": 1}))
        self.assertIn("tangerine ratio", got["text"])
        self.assertIn("\u25c7 scope", got["text"])

    def test_an_id_that_no_scope_holds_is_an_unknown_id(self):
        b = self.provider(self.beta, "sb")
        got = json.loads(b.handle_tool_call("crumb", {"id": "deadbeef", "level": 1}))
        self.assertIn("unknown id", got["error"])

    def test_scope_command_names_the_place_the_memory_and_the_others(self):
        a = self.provider(os.path.join(self.alpha, "src"), "sa")
        a.sync_turn("a line for the list", "yes", session_id="sa")
        self.provider(self.beta, "sb").sync_turn("beta line", "ok", session_id="sb")
        base = os.path.join(self.hermes, "breadcrumbs")
        text = workdir.command(base, a.scope_info, "")
        self.assertIn("here", text)
        self.assertIn("(git repository: ", text)
        self.assertIn(scope.short(self.beta), text)  # a scope with a memory is listed
        self.assertNotIn("profile", text.lower().replace("the profile's default", ""))
        self.assertIn('scope "up"', workdir.command(base, a.scope_info, "up"))
        self.assertTrue(workdir.command(base, a.scope_info, "bogus").startswith("usage:"))

    def test_the_scopes_register_a_command_and_the_start_hook_and_no_cli_or_profile(self):
        calls = []

        class Ctx:
            def register_command(self, name, fn, description="", args_hint=""):
                calls.append(name)

            def register_hook(self, *a, **k):
                calls.append("hook")

            def register_cli_command(self, *a, **k):
                calls.append("cli")

        workdir.register(Ctx(), None)
        self.assertEqual(calls, ["hook", "scope", "viz"])  # the hook: the explorer at the start of a terminal chat

    def test_a_scope_may_override_the_default_config(self):
        base = os.path.join(self.hermes, "breadcrumbs")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"neighbor": 0.1}, f)
        folder = scope.ensure(base, scope.GIT, self.alpha)
        with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"neighbor": 0.4}, f)
        self.assertAlmostEqual(self.provider(self.alpha, "sa").neighbor, 0.4)
        self.assertAlmostEqual(self.provider(self.beta, "sb").neighbor, 0.1)

    def test_scope_command_off_state_says_so(self):
        with mock.patch.dict(os.environ, {"BREADCRUMBS_SCOPE": "off"}):
            p = self.provider(self.alpha, "sa")
            text = workdir.command(os.path.join(self.hermes, "breadcrumbs"), p.scope_info, "")
        self.assertIn("OFF", text)


if __name__ == "__main__":
    unittest.main()
