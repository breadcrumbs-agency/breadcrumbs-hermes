"""compaction: which Hermes profiles compact with ACE, and the switch that sets it (breadcrumbs_hermes/compaction.py)."""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from breadcrumbs_hermes import compaction


class CompactionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        p = mock.patch.object(compaction, "_root", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

        def fake(profile, *args):  # `hermes -p PROFILE config set KEY VALUE`: write the key as Hermes does
            self.calls.append((profile,) + args)
            block, key = args[1].split(".")
            path = os.path.join(compaction.home_of(profile), "config.yaml")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if block + ":\n" not in text:
                text += "%s:\n" % block
            lines = [l for l in text.splitlines() if not l.strip().startswith(key + ":") or not l.startswith(" ")]
            out = []
            for l in lines:
                out.append(l)
                if l == block + ":":
                    out.append("  %s: %s" % (key, args[2]))
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
            return ""
        p2 = mock.patch.object(compaction, "hermes_config", fake)
        p2.start()
        self.addCleanup(p2.stop)

    def profile(self, name, config="", plugin=True):
        home = compaction.home_of(name)
        os.makedirs(os.path.join(home, "plugins", "breadcrumbs" if plugin else "x"), exist_ok=True)
        with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(config)

    def run_cmd(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = compaction.command(*args)
        return code, out.getvalue()

    def test_profiles_lists_the_default_and_each_profile_folder(self):
        self.profile("default")
        self.profile("b")
        self.profile("a")
        self.assertEqual(compaction.profiles(), ["default", "a", "b"])

    def test_a_profile_compacts_with_hermes_unless_both_settings_name_breadcrumbs(self):
        self.profile("none")
        self.profile("engine_only", "context:\n  engine: breadcrumbs\n")
        self.profile("memory_only", "memory:\n  provider: breadcrumbs\n")
        self.profile("both", "memory:\n  provider: breadcrumbs\ncontext:\n  engine: breadcrumbs\n")
        modes = {p: compaction.state(p)["mode"] for p in ("none", "engine_only", "memory_only", "both")}
        self.assertEqual(modes, {"none": "hermes", "engine_only": "hermes", "memory_only": "hermes", "both": "ace"})

    def test_status_changes_nothing(self):
        self.profile("a", "memory:\n  provider: breadcrumbs\n")
        code, out = self.run_cmd("status", [], False)
        self.assertEqual(code, 0)
        self.assertIn("compaction=hermes", out)
        self.assertEqual(self.calls, [])

    def test_on_sets_the_provider_before_the_engine(self):
        self.profile("a")
        code, out = self.run_cmd("on", ["a"], False)
        self.assertEqual(code, 0)
        self.assertEqual([c[2] for c in self.calls], ["memory.provider", "context.engine"])
        self.assertIn("compaction=ace", out)

    def test_on_needs_a_profile_or_all(self):
        self.profile("a")
        code, _ = self.run_cmd("on", [], False)
        self.assertEqual((code, self.calls), (2, []))

    def test_on_skips_a_profile_without_the_plugin(self):
        self.profile("bare", plugin=False)
        code, out = self.run_cmd("on", [], True)
        self.assertEqual((code, self.calls), (1, []))
        self.assertIn("skipped: no plugin", out)

    def test_off_returns_the_engine_seat_to_hermes_and_leaves_the_provider(self):
        self.profile("a", "memory:\n  provider: breadcrumbs\ncontext:\n  engine: breadcrumbs\n")
        self.profile("b", "context:\n  engine: lcm\n")
        code, out = self.run_cmd("off", [], False)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [("a", "set", "context.engine", "compressor")])  # b's engine is not ours
        self.assertEqual(compaction.state("a")["memory"], "breadcrumbs")
        self.assertEqual(compaction.state("b")["engine"], "lcm")

    def test_an_unknown_profile_is_an_error(self):
        self.profile("a")
        code, out = self.run_cmd("status", ["zz"], False)
        self.assertEqual(code, 1)
        self.assertIn("no such profile: zz", out)


class HealTest(unittest.TestCase):
    """The plugin fixes a profile that selects ACE without a way to open its folds, when it loads."""

    def home(self, config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(config)
        return tmp.name

    def heal(self, config, **env):
        home, sets = self.home(config), []
        with mock.patch.dict(os.environ, env):
            did = compaction.heal(home, setter=lambda k, v: sets.append((k, v)))
        return did, sets, home

    def test_an_engine_with_no_memory_provider_gets_the_provider(self):
        did, sets, home = self.heal("context:\n  engine: breadcrumbs\n")
        self.assertEqual((did, sets), ("filled", [("memory.provider", "breadcrumbs")]))
        with open(os.path.join(home, "breadcrumbs", "heal.log")) as f:
            self.assertIn("filled: context.engine is breadcrumbs", f.read())

    def test_an_engine_next_to_another_provider_goes_back_to_hermes(self):
        did, sets, _ = self.heal("memory:\n  provider: honcho\ncontext:\n  engine: breadcrumbs\n")
        self.assertEqual((did, sets), ("reverted", [("context.engine", "compressor")]))

    def test_a_right_or_unselected_profile_is_left_alone(self):
        for config in ("memory:\n  provider: breadcrumbs\ncontext:\n  engine: breadcrumbs\n",
                       "memory:\n  provider: breadcrumbs\ncontext:\n  engine: lcm\n",  # another engine keeps its seat
                       "memory:\n  provider: honcho\n", "context:\n  engine: lcm\n", ""):
            self.assertEqual(self.heal(config)[:2], (None, []), config)

    def test_the_plugin_takes_the_context_where_it_is_the_memory_provider(self):
        for config in ("memory:\n  provider: breadcrumbs\n", "memory:\n  provider: breadcrumbs\ncontext:\n  engine: compressor\n"):
            did, sets, home = self.heal(config)
            self.assertEqual((did, sets), ("took", [("context.engine", "breadcrumbs")]), config)
            with open(os.path.join(home, "breadcrumbs", "heal.log")) as f:
                self.assertIn("took: memory.provider is breadcrumbs and context.engine was", f.read())

    def test_taking_over_turns_off_the_overlapping_hermes_extras(self):
        did, sets, _ = self.heal("memory:\n  provider: breadcrumbs\ncompression:\n  enabled: true\n"
                                 "  idle_compact_after_seconds: 1800\n  proactive_prune_tokens: 0\n"
                                 "  prepare_ahead: true\n  codex_responses_native: false\n")
        self.assertEqual(did, "took")
        self.assertEqual(sets, [("context.engine", "breadcrumbs"), ("compression.idle_compact_after_seconds", "0"),
                                ("compression.prepare_ahead", "false")])  # enabled stays: Hermes asks no engine without it

    def test_the_takeover_dial_and_the_environment_leave_the_profile_alone(self):
        home = self.home("memory:\n  provider: breadcrumbs\n")
        os.makedirs(os.path.join(home, "breadcrumbs"))
        with open(os.path.join(home, "breadcrumbs", "config.json"), "w") as f:
            f.write('{"takeover": false}')
        sets = []
        self.assertIsNone(compaction.heal(home, setter=lambda k, v: sets.append((k, v))))
        self.assertEqual(sets, [])
        home2 = self.home("memory:\n  provider: breadcrumbs\n")
        with mock.patch.dict(os.environ, {"BREADCRUMBS_TAKEOVER": "0"}):
            self.assertIsNone(compaction.heal(home2, setter=lambda k, v: sets.append((k, v))))
        self.assertEqual(sets, [])

    def test_the_opt_out_leaves_a_wrong_profile_alone(self):
        self.assertEqual(self.heal("context:\n  engine: breadcrumbs\n", BREADCRUMBS_NO_HEAL="1")[:2], (None, []))

    def test_a_failing_writer_does_not_fail_the_load(self):
        home = self.home("context:\n  engine: breadcrumbs\n")

        def boom(k, v):
            raise RuntimeError("no hermes here")
        self.assertIsNone(compaction.heal(home, setter=boom))


if __name__ == "__main__":
    unittest.main()
