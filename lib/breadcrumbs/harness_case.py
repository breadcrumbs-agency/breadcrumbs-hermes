"""The test cases of a harness, for the tests of the packages above common: HarnessCase (their tests/test_adapter.py) and
SourceCase (tests/test_sessions.py of a harness that gives a session source).

A package fills the values and gets the five checks of an adapter: it gives one entry to the harness table, identify
names its clients, the table lists it, its setup text is the one expected, and the health count names its calls. The
test module imports THIS MODULE, not the class, so that unittest discover does not run the empty case by itself:

    from breadcrumbs import harness_case

    class Adapter(harness_case.HarnessCase):
        KEY, LABEL, LAYER = "codex", "Codex", "codex"
        NAMES = ["codex-mcp-client", "codex"]
        SETUP = "codex mcp add breadcrumbs -- env PYTHONPATH=/x python -m breadcrumbs core mcp"

SourceCase is used the same way (see its docstring).
"""
import contextlib
import io
import json
import os
import tempfile
import unittest

from breadcrumbs import cli, core, ports, usage
from breadcrumbs.store import Store


class HarnessCase(unittest.TestCase):
    KEY = LABEL = LAYER = SETUP = None  # the key of the harness table, its label, the name of its layer, the setup text for CMD
    NAMES = ()  # client names that identify the harness
    CMD = ["env", "PYTHONPATH=/x", "python", "-m", "breadcrumbs", "core", "mcp"]

    def setUp(self):
        self.entry = ports.harnesses()[self.KEY]

    def test_the_package_gives_one_entry_to_the_table(self):
        self.assertEqual(self.entry["label"], self.LABEL)
        self.assertEqual(ports.layer(self.LAYER, __file__), False)  # the adapter announced its layer once

    def test_identify_tells_the_client_names_apart(self):
        for name in self.NAMES:
            self.assertEqual(core.identify(name), self.KEY, name)
        for name in ("curl", "", None):
            self.assertEqual(core.identify(name), "unknown", name)

    def test_the_table_lists_it_in_its_place(self):
        self.assertIn(self.KEY, core.harnesses())

    def test_setup_is_the_text_that_adds_the_server(self):
        self.assertEqual(self.entry["setup"](self.CMD), self.SETUP)

    def test_health_counts_the_calls_of_this_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = core.Core(core.Stores(root=tmp))
            try:
                c.call("stores", harness=self.KEY)
                self.assertEqual(c.call("health")["calls"], {self.KEY: 1, "unknown": 1})
            finally:
                c.close()


class SourceCase(unittest.TestCase):
    """The test case of a harness that gives a session source (a part's tests/test_sessions.py): the part fills KEY
    and fixture(), and gets the check of the contract of `ingest`. Its own tests read self.sessions() and self.path.

        class Sessions(harness_case.SourceCase):
            KEY = "codex"
            def fixture(self, folder): ...  # write a small file with invented text; return its path

    A harness that gives usage gets one more check: the first run adds a usage.log line for each request of the fixture
    (src=KEY), and a second run adds none.
    """
    KEY = None  # the key of the harness table

    def fixture(self, folder):
        raise NotImplementedError

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.join(tmp.name, "home")
        self.path = self.fixture(tmp.name)

    def sessions(self):
        """The sessions that the source of the harness reads from the fixture."""
        return list(ports.harnesses()[self.KEY]["source"](self.path))

    def ingest(self):
        """`ingest KEY PATH` into the temporary home: its stats, and the count of blobs in the store."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", self.home, "ingest", self.KEY, self.path]), 0)
        store = Store(os.path.join(self.home, "store"), readonly=True)
        try:
            return json.loads(out.getvalue()), len(store)
        finally:
            store.close()

    def test_a_second_run_adds_only_the_commit(self):
        stats, blobs = self.ingest()
        self.assertGreater(stats["sessions"], 0)
        self.assertEqual(self.ingest()[1], blobs + 1)

    def usage_lines(self):
        """The lines of usage.log in the temporary home."""
        try:
            with open(os.path.join(self.home, usage.LOG_NAME), encoding="utf-8") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_a_second_run_adds_no_usage_record(self):
        if ports.harnesses()[self.KEY].get("usage") is None:
            return  # the harness gives no usage
        first = self.ingest()[0]["usage"]
        lines = self.usage_lines()
        self.assertEqual((first["added"], len(lines)), (first["requests"], first["requests"]))
        self.assertGreater(len(lines), 0, "the fixture has requests")
        self.assertTrue(all(line.endswith(" src=" + self.KEY) for line in lines))
        again = self.ingest()[0]["usage"]
        self.assertEqual((again["added"], again["requests"]), (0, first["requests"]))
        self.assertEqual(self.usage_lines(), lines, "dedupe by request id")
