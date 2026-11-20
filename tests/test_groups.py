"""Groups of profiles: profiles inherit settings, never memory, unless they are grouped."""
import json
import os
import tempfile
import unittest
from unittest import mock

import _scope_off  # noqa: F401
from breadcrumbs import dials, scope
from breadcrumbs_hermes import config_layers, groups, workdir


def write(folder, cfg):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "config.json"), "w") as f:
        json.dump(cfg, f)


class Groups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hermes = os.path.realpath(self.tmp.name)
        open(os.path.join(self.hermes, "config.yaml"), "w").close()
        self.base = {"default": os.path.join(self.hermes, "breadcrumbs")}
        for name in ("a", "b", "c"):
            self.base[name] = os.path.join(self.hermes, "profiles", name, "breadcrumbs")
            os.makedirs(self.base[name])
        os.makedirs(self.base["default"], exist_ok=True)

    def chain(self, name):
        with mock.patch.dict(os.environ, {"BREADCRUMBS_SCOPE": "auto"}):
            return workdir.config_folders(self.base[name], {"cwd": self.tmp.name})

    def test_crud(self):
        groups.create(self.hermes, "perf", "cheap lanes")
        with self.assertRaises(ValueError):
            groups.create(self.hermes, "perf")
        with self.assertRaises(ValueError):
            groups.create(self.hermes, "bad name")
        groups.add(self.hermes, "perf", "a")
        groups.add(self.hermes, "perf", "b")
        with self.assertRaises(ValueError):
            groups.add(self.hermes, "perf", "nobody")
        with self.assertRaises(ValueError):
            groups.add(self.hermes, "perf", "default")
        got = groups.apply(self.hermes, "get")
        self.assertEqual((got["groups"][0]["name"], got["groups"][0]["profiles"], got["ungrouped"]),
                         ("perf", ["a", "b"], ["c"]))
        groups.create(self.hermes, "other")
        groups.add(self.hermes, "other", "b")  # one group at most: b leaves perf
        self.assertEqual((groups.members(self.hermes, "perf"), groups.group_of(self.hermes, "b")), (["a"], "other"))
        groups.rename(self.hermes, "perf", "fast")
        self.assertEqual(groups.group_of(self.hermes, "a"), "fast")
        groups.remove(self.hermes, "fast", "a")
        self.assertIsNone(groups.group_of(self.hermes, "a"))
        with self.assertRaises(ValueError):
            groups.remove(self.hermes, "fast", "a")
        with self.assertRaises(KeyError):
            groups.apply(self.hermes, "delete", {"name": "nope"})

    def test_delete_keeps_the_config(self):
        groups.create(self.hermes, "perf")
        write(groups.folder(self.hermes, "perf"), {"ace_tokens": 111000})
        groups.add(self.hermes, "perf", "a")
        kept = groups.delete(self.hermes, "perf")
        self.assertTrue(os.path.isfile(os.path.join(kept, "config.json")))
        self.assertFalse(os.path.exists(groups.folder(self.hermes, "perf")))
        self.assertIsNone(groups.group_of(self.hermes, "a"))

    def test_a_bad_file_is_no_groups(self):
        with open(os.path.join(self.hermes, "breadcrumbs", "groups.json"), "w") as f:
            f.write("{")
        self.assertEqual(groups.load(self.hermes), {})

    def test_profiles_inherit_the_default_and_a_group_sits_between(self):
        write(self.base["default"], {"ace_tokens": 300000, "neighbor": 0.2})
        groups.create(self.hermes, "perf")
        write(groups.folder(self.hermes, "perf"), {"ace_tokens": 200000})
        groups.add(self.hermes, "perf", "a")
        write(self.base["b"], {"neighbor": 0.9})
        self.assertEqual([l for l, _ in self.chain("default")], ["profile"])
        self.assertEqual([l for l, _ in self.chain("a")], ["default", "group perf", "profile"])
        self.assertEqual([l for l, _ in self.chain("b")], ["default", "profile"])
        with mock.patch.dict(os.environ, {"BREADCRUMBS_SCOPE": "auto"}):
            a = workdir.config_chain(self.base["a"], {"cwd": self.tmp.name})
            b = workdir.config_chain(self.base["b"], {"cwd": self.tmp.name})
            c = workdir.config_chain(self.base["c"], {"cwd": self.tmp.name})
        self.assertEqual((dials.value(a, None, "ace_tokens", {}), dials.value(a, None, "neighbor", {})), (200000, 0.2))
        self.assertEqual((dials.value(b, None, "ace_tokens", {}), dials.value(b, None, "neighbor", {})), (300000, 0.9))
        self.assertEqual(dials.value(c, None, "ace_tokens", {}), 300000)  # no file of its own: the default's

    def test_memory_is_shared_only_inside_a_group(self):
        for name in ("a", "b", "c"):
            open(os.path.join(self.base[name], "index.db"), "w").close()
        info = {"store": self.base["a"]}
        self.assertEqual(workdir.stores_of_group(self.base["a"]), [])
        self.assertFalse(groups.may_read(self.hermes, "a", "b"))
        groups.create(self.hermes, "perf")
        groups.add(self.hermes, "perf", "a")
        groups.add(self.hermes, "perf", "b")
        self.assertEqual([x[0] for x in workdir.stores_of_group(self.base["a"])], ["profile b"])
        self.assertEqual([x[2] for x in workdir.stores_of_group(self.base["b"])], [self.base["a"]])
        self.assertEqual(workdir.stores_of_group(self.base["c"]), [])  # c is in no group: it sees nobody
        self.assertEqual(workdir.stores_of_group(self.base["default"]), [])
        self.assertTrue(groups.may_read(self.hermes, "a", "b"))
        self.assertFalse(groups.may_read(self.hermes, "a", "c"))
        self.assertIn(self.base["b"], [x[2] for x in workdir.stores_all(self.base["a"], info)])
        self.assertNotIn(self.base["c"], [x[2] for x in workdir.stores_all(self.base["a"], info)])

    def test_the_explorer_lists_the_group_between_the_default_and_its_members(self):
        groups.create(self.hermes, "perf")
        groups.add(self.hermes, "perf", "a")
        got = {l["id"]: l for l in config_layers.layers(self.base["a"])}
        self.assertEqual(got["profile:default"]["parent"], None)
        self.assertEqual(got["group:perf"]["parent"], "profile:default")
        self.assertEqual(got["profile:a"]["parent"], "group:perf")
        self.assertEqual(got["profile:b"]["parent"], "profile:default")
        self.assertEqual(got["group:perf"]["path"], groups.folder(self.hermes, "perf"))


if __name__ == "__main__":
    unittest.main()
