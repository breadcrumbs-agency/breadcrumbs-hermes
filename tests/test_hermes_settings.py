"""The Hermes section of the settings widget: the two switches, their writes to hermes config, and the revert record.
hermes config is replaced by a fake that keeps a dict, so no profile is written."""
import json
import os
import tempfile
import unittest
from unittest import mock

from breadcrumbs_hermes import hermes_settings as hs

REAL_CONFIG = hs._config  # kept before setUp patches it, so one test can run the real wrapper


class FakeConfig:
    def __init__(self, values):
        self.values = dict(values)
        self.calls = []

    def __call__(self, profile, *args):
        self.calls.append((profile,) + args)
        if args[0] == "get":
            return self.values.get(args[1], "")
        if args[0] == "set":
            self.values[args[1]] = args[2]
            return args[2]
        raise AssertionError(args)


class HermesSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "profiles", "alpha", "breadcrumbs", "scopes", "app-1")
        os.makedirs(self.root)
        self.fake = FakeConfig({"memory.provider": "mem0", "context.engine": "compressor"})
        patch = mock.patch.object(hs, "_config", self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    def fields(self, view):
        return {f["key"]: f for f in view["fields"]}

    def test_the_profile_and_the_base_come_from_the_path(self):
        self.assertEqual(hs.profile_of(self.root), "alpha")
        self.assertEqual(hs.base_of(self.root), os.path.join(self.tmp.name, "profiles", "alpha", "breadcrumbs"))
        self.assertEqual(hs.profile_of("/home/x/breadcrumbs"), "default")

    def test_get_reads_each_switch_from_hermes_config(self):
        f = self.fields(hs.settings(self.root, "get"))
        self.assertFalse(f["memory_provider"]["value"])
        self.assertFalse(f["context_engine"]["value"])
        self.assertFalse(f["memory_provider"]["overridden"])
        self.assertEqual(f["memory_provider"]["source"], "hermes config (alpha)")

    def test_on_saves_the_earlier_value_and_writes_breadcrumbs(self):
        view = hs.settings(self.root, "set", {"key": "context_engine", "value": True})
        self.assertEqual(self.fake.values["context.engine"], "breadcrumbs")
        self.assertTrue(self.fields(view)["context_engine"]["value"])
        self.assertTrue(self.fields(view)["context_engine"]["overridden"])
        with open(os.path.join(hs.base_of(self.root), "hermes-settings.json")) as fh:
            self.assertEqual(json.load(fh), {"context_engine": "compressor"})

    def test_revert_restores_the_earlier_value(self):
        hs.settings(self.root, "set", {"key": "memory_provider", "value": True})
        view = hs.settings(self.root, "delete", {"key": "memory_provider"})
        self.assertEqual(self.fake.values["memory.provider"], "mem0")
        self.assertFalse(self.fields(view)["memory_provider"]["overridden"])

    def test_turning_on_twice_keeps_the_first_earlier_value(self):
        hs.settings(self.root, "set", {"key": "memory_provider", "value": True})
        hs.settings(self.root, "set", {"key": "memory_provider", "value": True})
        hs.settings(self.root, "delete", {"key": "memory_provider"})
        self.assertEqual(self.fake.values["memory.provider"], "mem0")

    def test_off_without_a_record_is_refused_with_the_command(self):
        with self.assertRaisesRegex(ValueError, "hermes -p alpha config set context.engine NAME"):
            hs.settings(self.root, "set", {"key": "context_engine", "value": False})
        self.assertEqual(self.fake.values["context.engine"], "compressor")

    def test_an_empty_earlier_value_is_not_reverted_by_the_widget(self):
        self.fake.values.pop("context.engine")
        hs.settings(self.root, "set", {"key": "context_engine", "value": True})
        with self.assertRaisesRegex(ValueError, "no command that unsets"):
            hs.settings(self.root, "delete", {"key": "context_engine"})
        self.assertEqual(self.fake.values["context.engine"], "breadcrumbs")

    def test_reset_reverts_every_switch_the_widget_turned_on(self):
        hs.settings(self.root, "set", {"key": "memory_provider", "value": True})
        hs.settings(self.root, "set", {"key": "context_engine", "value": True})
        view = hs.settings(self.root, "reset")
        self.assertEqual(self.fake.values["memory.provider"], "mem0")
        self.assertEqual(self.fake.values["context.engine"], "compressor")
        self.assertFalse(any(f["overridden"] for f in view["fields"]))

    def test_bad_input_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown Hermes setting"):
            hs.settings(self.root, "set", {"key": "nope", "value": True})
        with self.assertRaisesRegex(ValueError, "true or false"):
            hs.settings(self.root, "set", {"key": "memory_provider", "value": "yes"})
        with self.assertRaisesRegex(ValueError, "unknown Hermes settings action"):
            hs.settings(self.root, "wipe")

    def test_a_failed_hermes_command_is_a_value_error_not_an_exit(self):
        def failing(profile, *args):
            raise SystemExit("breadcrumbs: hermes config get failed")
        with mock.patch.object(hs, "_config", REAL_CONFIG), \
                mock.patch("breadcrumbs_hermes.adapter.hermes_config", failing):
            with self.assertRaisesRegex(ValueError, "hermes config get failed"):
                hs.settings(self.root, "get")


if __name__ == "__main__":
    unittest.main()
