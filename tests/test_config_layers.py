"""The config files of a Hermes root that the explorer shows (config_layers.py), and the chain that a chat reads
(workdir.config_folders): the profile's file, then its scope's, for the provider, ACE and the request options alike."""
import json
import os
import tempfile
import unittest
from unittest import mock

import _scope_off  # noqa: F401
from breadcrumbs import dials, scope
from breadcrumbs_hermes import config_layers, novis, workdir


class Layers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hermes = os.path.realpath(self.tmp.name)
        open(os.path.join(self.hermes, "config.yaml"), "w").close()
        for name in ("work", "bot", "hidden"):
            os.makedirs(os.path.join(self.hermes, "profiles", name, "breadcrumbs"))
        self.repo = os.path.join(self.hermes, "src", "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.work = os.path.join(self.hermes, "profiles", "work", "breadcrumbs")
        self.scoped = scope.ensure(self.work, scope.GIT, self.repo)
        scope.ensure(os.path.join(self.hermes, "profiles", "hidden", "breadcrumbs"), scope.GIT, self.repo)
        novis.save({"profiles": ["hidden"], "sessions": []}, self.hermes)

    def test_every_profile_and_each_scope_with_the_chats_that_read_it(self):
        got = config_layers.layers(self.scoped)
        ids = [l["id"] for l in got]
        self.assertEqual(ids[:3], ["profile:default", "profile:bot", "profile:work"])
        sid = "scope:work:" + scope.scope_id(self.repo)
        self.assertEqual(ids[3:], [sid])  # the scope of the profile that opted out of the viewers is not listed
        layer = got[3]
        self.assertEqual((layer["parent"], layer["here"], layer["path"]), ("profile:work", True, self.scoped))
        self.assertIn("gateway bots", got[1]["applies"])
        self.assertIn("a git repository", layer["applies"])
        self.assertEqual(config_layers.hermes_of(self.work), self.hermes)

    def test_a_store_outside_hermes_has_no_layers(self):
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(config_layers.layers(other), [])


class Chain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "home", "breadcrumbs")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        os.makedirs(self.base)
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump({"ace_tokens": 300000, "neighbor": 0.2}, f)

    def test_a_chat_in_a_scope_reads_the_scope_file_over_the_profiles(self):
        scoped = scope.ensure(self.base, scope.GIT, self.repo)
        with open(os.path.join(scoped, "config.json"), "w") as f:
            json.dump({"ace_tokens": 120000}, f)
        with mock.patch.dict(os.environ, {"BREADCRUMBS_SCOPE": "auto"}):
            folders = workdir.config_folders(self.base, {"cwd": self.repo})
            chain = workdir.config_chain(self.base, {"cwd": self.repo})
        self.assertEqual(folders, [("profile", self.base), ("scope", scoped)])
        self.assertEqual(dials.value(chain, None, "ace_tokens", {}), 120000)
        self.assertEqual(dials.value(chain, None, "neighbor", {}), 0.2)

    def test_no_scope_store_no_folder_made_and_the_profiles_file_alone(self):
        with mock.patch.dict(os.environ, {"BREADCRUMBS_SCOPE": "auto"}):
            self.assertEqual(workdir.config_folders(self.base, {"cwd": self.repo}), [("profile", self.base)])
        self.assertFalse(os.path.isdir(os.path.join(self.base, "scopes")))
        self.assertEqual(workdir.config_chain(self.base, info={"store": self.base}), dials.config(self.base))

    def test_ace_reads_the_same_chain(self):
        from breadcrumbs_hermes import ace
        scoped = scope.ensure(self.base, scope.GIT, self.repo)
        with open(os.path.join(scoped, "config.json"), "w") as f:
            json.dump({"ace_tokens": 120000}, f)
        engine = ace.Ace()
        with mock.patch.dict(os.environ, {"BREADCRUMBS_SCOPE": "auto"}), \
                mock.patch.object(workdir, "chat_cwd", lambda kwargs=None: self.repo):
            self.assertEqual(engine.dial_values(self.base, "")["ace_tokens"], 120000)


if __name__ == "__main__":
    unittest.main()
