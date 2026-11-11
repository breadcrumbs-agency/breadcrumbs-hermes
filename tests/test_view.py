import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import view
from breadcrumbs.hermes import BreadcrumbsProvider

SESSIONS = {
    "tea1": [("how long should green tea steep in the kettle water", "steep green tea two minutes; kettle water near 80 C")],
    "tea2": [("the kettle boils too fast for black tea", "black tea likes the kettle water just off the boil")],
    "bike": [("my bike chain skips on the small cog", "replace the worn chain and check every cog for wear")],
}


def ids(rows):
    return [r["id"] for r in rows]


class TrailTest(unittest.TestCase):
    """The trail is SpaceGraph's zoom stack: push a new crumb, keep 8, show the rest as one gap."""

    def test_a_new_crumb_is_pushed_and_the_oldest_leaves_after_eight(self):
        t = view.Trail()
        for i in range(10):
            t.visit("c%07d" % i, 0.1)
        self.assertEqual(ids(t.stack), ["c%07d" % i for i in range(2, 10)])
        self.assertEqual(t.dropped, 2)

    def test_the_top_crumb_again_changes_only_its_level(self):
        t = view.Trail()
        t.visit("a0000000", 0.1)
        t.visit("b0000000", 0.1)
        t.visit("b0000000", 0.8)
        self.assertEqual([(e["id"], e["level"]) for e in t.stack], [("a0000000", 0.1), ("b0000000", 0.8)])

    def test_a_crumb_lower_in_the_stack_goes_back_to_it(self):
        t = view.Trail()
        for c in ("a0000000", "b0000000", "c0000000"):
            t.visit(c, 0.2)
        t.visit("a0000000", 0.5)
        self.assertEqual([(e["id"], e["level"]) for e in t.stack], [("a0000000", 0.5)])

    def test_the_status_line_runs_oldest_to_newest_with_a_gap_mark(self):
        t = view.Trail()
        for i in range(9):
            t.visit("c%07d" % i, 0.1, "turn: n%d" % i)
        line = t.line()
        self.assertTrue(line.startswith("\u2026 \u203a "))
        self.assertTrue(line.endswith("turn: n8"))
        self.assertEqual(view.Trail.from_dict(json.loads(json.dumps(t.to_dict()))).line(), line)


class ViewData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # first in, last out: after every View closes (Windows)
        self.p = BreadcrumbsProvider()
        self.p.initialize("tea1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        for sid, turns in SESSIONS.items():
            for u, a in turns:
                self.p.sync_turn(u, a, session_id=sid)
        self.sess = {sid: self.p.session_crumb(sid) for sid in SESSIONS}
        self.root = os.path.join(self.tmp.name, "breadcrumbs")

    def tearDown(self):
        self.p.shutdown()

    def open(self):
        """A View that the test closes (Windows cannot remove a folder with open files)."""
        v = view.open_view(self.root)
        self.addCleanup(v.close)
        return v


class ReadOnlyViewTest(ViewData):
    def test_a_folder_without_a_store_raises_and_gets_no_file(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.mkdir(empty)
        for path in (os.path.join(self.tmp.name, "nope"), empty):
            with self.assertRaises(FileNotFoundError):
                view.View(path)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "nope")))
        self.assertEqual(os.listdir(empty), [])

    def test_a_view_changes_no_file_and_close_releases_it(self):
        self.p.shutdown()
        before = snapshot(self.root)
        v = view.View(self.root)
        v.graph()
        v.find("kettle")
        v.crumb(self.sess["tea1"], 0.5)
        v.close()
        v.close()  # a second close does nothing
        self.assertTrue(v.store._reader.closed)
        self.assertEqual(snapshot(self.root), before)


class GraphTest(ViewData):
    def test_one_node_per_session_with_cue_words_and_a_size(self):
        g = self.open().graph()
        nodes = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(set(nodes), set(self.sess.values()))
        tea = nodes[self.sess["tea1"]]
        self.assertEqual((tea["kind"], tea["session"]), ("sess", "tea1"))
        self.assertIn("tea", tea["cues"])
        self.assertGreater(tea["tokens"], 0)
        self.assertTrue(all(0 < n["pri"] <= 1 for n in g["nodes"]))

    def test_sessions_that_share_cue_words_are_joined(self):
        g = self.open().graph()
        pairs = {frozenset((e["source"], e["target"])) for e in g["edges"] if e["kind"] == "cue"}
        self.assertIn(frozenset((self.sess["tea1"], self.sess["tea2"])), pairs)
        self.assertFalse(any(self.sess["bike"] in p for p in pairs))

    def test_a_root_opens_one_level_of_parts(self):
        g = self.open().graph(root=self.sess["bike"])
        kids = [e["target"] for e in g["edges"] if e["kind"] == "part" and e["source"] == self.sess["bike"]]
        self.assertEqual(len(kids), 2)
        self.assertTrue({"turn"} <= {n["kind"] for n in g["nodes"]})

    def test_find_groups_hits_by_session(self):
        hits = self.open().find("bike chain cog")
        self.assertEqual(hits[0]["session"], self.sess["bike"])
        self.assertTrue(hits[0]["hits"])

    def test_the_last_moved_sessions_come_first_and_the_limit_keeps_them(self):
        refs = os.path.join(self.root, "refs")
        for i, sid in enumerate(("bike", "tea1", "tea2")):  # tea2 moved last
            os.utime(os.path.join(refs, "sess-" + sid), (1000 + i, 1000 + i))
        g = self.open().graph(limit=2)
        self.assertEqual(ids(g["nodes"]), [self.sess["tea2"], self.sess["tea1"]])


class PathTest(ViewData):
    """The page follows the agent into a crumb: it needs the crumbs from the session down to it."""

    def test_a_serving_view_does_not_block_the_provider(self):
        v = self.open()
        v.graph()
        v.graph(root=self.sess["bike"])
        v.find("kettle")
        v.crumb(self.sess["tea1"], 0.5)
        v.path(self.sess["tea2"])
        self.p.index.db.execute("PRAGMA busy_timeout = 300")
        out = json.loads(self.p.handle_tool_call("mcp__crumb", {"id": self.sess["tea2"], "level": 0.5}))
        self.assertIn("text", out)
        self.p.sync_turn("one more question about tea", "one more answer", session_id="tea2")

    def test_a_turn_path_starts_at_its_session(self):
        v = self.open()
        sess = self.sess["bike"]
        turn = v.graph(root=sess)["nodes"][2]["id"]
        self.assertEqual(v.path(turn), [sess, turn])

    def test_a_session_path_is_the_session(self):
        sess = self.sess["tea2"]
        self.assertEqual(self.open().path(sess), [sess])

    def test_a_text_range_path_goes_through_its_turn_from_a_given_start(self):
        v = self.open()
        sess = self.sess["bike"]
        turn = v.graph(root=sess)["nodes"][1]["id"]
        text = v.graph(root=turn)["nodes"][1]["id"]
        self.assertIn(":", text)
        self.assertEqual(v.path(text), [])  # a text does not know its session
        self.assertEqual(v.path(text, within=sess), [sess, turn, text])


class ProviderTrailTest(ViewData):
    def test_crumb_calls_write_the_trail_file(self):
        self.p.handle_tool_call("mcp__crumb", {"id": self.sess["tea2"], "level": 0.2})
        self.p.handle_tool_call("mcp__crumb", {"id": self.sess["bike"], "level": 0.5})
        with open(os.path.join(self.root, "trail.json")) as f:
            t = json.load(f)
        self.assertEqual(ids(t["stack"]), [self.sess["tea2"], self.sess["bike"]])
        self.assertTrue(t["stack"][-1]["label"].startswith("sess"))
        self.assertEqual(self.open().trail()["stack"], t["stack"])


class ServerTest(ViewData):
    def setUp(self):
        super().setUp()
        self.server = view.make_server(self.root, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.token = view.token(self.root)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path, token=True):
        sep = "&" if "?" in path else "?"
        url = self.base + path + (sep + "k=" + self.token if token else "")
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def test_the_api_needs_the_token(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/graph", token=False)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()
        status, kind, body = self.get("/api/graph")
        self.assertEqual((status, kind.split(";")[0]), (200, "application/json"))
        self.assertEqual(len(json.loads(body)["nodes"]), 3)

    def test_the_page_is_served_without_the_token(self):
        status, kind, body = self.get("/", token=False)
        self.assertEqual((status, kind.split(";")[0]), (200, "text/html"))
        self.assertIn(b"<canvas", body)

    def test_crumb_renders_at_a_level(self):
        sess = self.sess["bike"]
        _, _, body = self.get("/api/crumb?id=%s&level=1" % sess)
        self.assertIn("my bike chain skips", json.loads(body)["text"])
        _, _, body = self.get("/api/crumb?id=%s&level=0" % sess)
        self.assertEqual(len(json.loads(body)["text"].splitlines()), 1)

    def test_path_and_an_unknown_id(self):
        sess = self.sess["tea1"]
        _, _, body = self.get("/api/path?id=" + sess)
        self.assertEqual(json.loads(body), [sess])
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/crumb?id=0badf00d&level=1")
        self.assertEqual(e.exception.code, 404)
        e.exception.close()

    def test_reads_change_no_file(self):
        self.p.shutdown()
        before = snapshot(self.root)
        for path in ("/api/graph", "/api/graph?root=" + self.sess["tea1"], "/api/find?q=kettle",
                     "/api/crumb?id=%s&level=0.5" % self.sess["tea1"], "/api/trail"):
            self.get(path)
        self.assertEqual(snapshot(self.root), before)


def snapshot(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            st = os.stat(os.path.join(base, name))
            out[os.path.join(base, name)] = (st.st_size, st.st_mtime_ns)
    return out



class BcCommandTest(ViewData):
    """`bc` is the dispatch handbook's way for a helper without the crumb tool (a delegate_task child): it
    runs beside a live gateway, so it reads like the View and shows what the crumb tool shows."""

    def bc(self, *argv):
        import contextlib
        import io
        from breadcrumbs import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--home", self.root, "bc", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_bc_shows_what_the_crumb_tool_shows_and_changes_no_file(self):
        tool = json.loads(self.p.handle_tool_call("crumb", {"id": self.sess["tea1"], "level": 1}))["text"]
        self.p.shutdown()
        before = snapshot(self.root)
        code, out, _ = self.bc(self.sess["tea1"], "1")
        self.assertEqual(code, 0)
        self.assertEqual(out, tool)
        self.assertIn("steep green tea two minutes", out)
        self.assertEqual(snapshot(self.root), before)

    def test_bc_answers_while_the_gateway_still_holds_the_store(self):
        code, out, _ = self.bc(self.sess["bike"], "1")
        self.assertEqual(code, 0)
        self.assertIn("replace the worn chain", out)

    def test_bc_on_a_folder_without_a_store_says_so_and_makes_no_file(self):
        self.root = os.path.join(self.tmp.name, "nope")
        code, _, err = self.bc("deadbeef", "1")
        self.assertEqual(code, 1)
        self.assertIn("no store", err)
        self.assertFalse(os.path.exists(self.root))

    def test_bc_names_an_id_that_the_store_does_not_have(self):
        code, _, err = self.bc("deadbeef", "1")
        self.assertEqual(code, 1)
        self.assertIn("deadbeef", err)


def ace_line(sess, minute, act, raw, sent, last=0, base=500, layer=0, lay=0, target=1000, folds=0):
    """One whole line of ace.log, as Ace._log writes it."""
    return ("2026-01-02T03:%02d:00 pid=7 sess=%s act=%s in=%d out=%d last=%d base=%s tpb=0.3000 r=0.3000 folds=%d "
            "layer=%d target=%d gain=1.0000 lay=%d\n") % (minute, sess, act, raw, sent, last, base, folds, layer, target, lay)


class AceTrackTest(ViewData):
    """The size of the context from ace.log: the points, the candles, the pie and the totals of every session."""
    S1, S2 = "local-0123456789abcdef01", "local-fedcba9876543210ff"

    def log(self, *lines, name="ace.log"):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write("".join(lines))

    def sawtooth(self):
        # grows to 1,000 (the target), a fold cuts it to 900 (10%), it grows again, a second fold cuts 850 of 950
        return [ace_line(self.S1, 0, "pass", 400, 400), ace_line(self.S1, 1, "pass", 700, 700, last=420),
                ace_line(self.S1, 2, "pass", 990, 990, last=710),
                ace_line(self.S1, 3, "fold", 1200, 700, last=1000, layer=9, lay=120, folds=1),
                ace_line(self.S1, 4, "keep", 1300, 800, last=690, layer=9, lay=120, folds=1),
                ace_line(self.S1, 5, "keep", 1500, 950, last=810, layer=9, lay=120, folds=1),
                ace_line(self.S1, 6, "fold", 1600, 855, last=960, layer=11, lay=140, folds=2)]

    def test_each_request_is_a_point_and_a_fold_is_a_drop_of_the_sent_size(self):
        self.log(*self.sawtooth())
        d = self.open().ace()
        pts = d["points"]
        self.assertEqual([p["act"] for p in pts], ["pass", "pass", "pass", "fold", "keep", "keep", "fold"])
        self.assertEqual([p["sent"] for p in pts], [400, 700, 990, 700, 800, 950, 855])
        self.assertEqual(pts[3]["drop"], round(1 - 700 / 990, 4))  # the first fold: 29% of the request before
        self.assertEqual(pts[6]["drop"], 0.1)                      # a gentle fold: a tenth
        self.assertEqual(pts[4]["drop"], 0.0)                      # a request that grew cuts nothing
        self.assertEqual(d["session"], d["current"])
        self.assertEqual(d["target"], 1000)

    def test_the_models_count_of_a_request_is_read_from_the_request_after_it(self):
        self.log(*self.sawtooth())
        pts = self.open().ace()["points"]
        self.assertEqual([p["seen"] for p in pts], [420, 710, 1000, 690, 810, 960, None])

    def test_a_session_matches_by_the_prefix_that_a_log_line_holds(self):
        self.log(*self.sawtooth(), ace_line(self.S2, 9, "pass", 100, 100))
        v = self.open()
        self.assertEqual(v.ace(self.S1 + "abcdef0123456789")["session"], self.S1)  # the full session id of Hermes
        self.assertEqual(v.ace()["session"], self.S2)                              # the newest line
        none = v.ace("local-00000000000000000000ffff")
        self.assertEqual((none["session"], none["points"], none["mix"]), ("", [], None))
        self.assertEqual(none["all"]["sessions"], 2)

    def test_candles_are_open_high_low_close_of_the_sent_size(self):
        self.log(*self.sawtooth())
        c = self.open().ace(per=3)["candles"]
        self.assertEqual([(x["open"], x["high"], x["low"], x["close"], x["folds"], x["n"]) for x in c],
                         [(400, 990, 400, 990, 0, 3), (700, 950, 700, 950, 1, 3), (855, 855, 855, 855, 1, 1)])
        self.assertEqual(len(self.open().ace()["candles"]), 7)  # few requests: one candle each

    def test_the_pie_splits_the_last_request_into_base_layer_exact_and_held(self):
        self.log(*self.sawtooth())
        mix = self.open().ace()["mix"]
        self.assertEqual((mix["base"], mix["layer"], mix["exact"], mix["sent"], mix["raw"], mix["held"]),
                         (500, 140, 855 - 500 - 140, 855, 1600, 1600 - 855))
        self.assertEqual(mix["unknown"], [])

    def test_a_line_from_before_the_layer_size_was_logged_names_what_it_does_not_know(self):
        old = ("2026-01-02T03:00:00 pid=7 sess=%s act=keep in=900 out=600 last=0 base=100 tpb=0.3000 r=0.3000 "
               "folds=1 layer=4\n") % self.S1
        self.log(old)
        mix = self.open().ace()["mix"]
        self.assertEqual((mix["layer"], mix["exact"], mix["unknown"]), (0, 500, ["lay"]))

    def test_the_totals_bin_every_fold_by_the_share_it_cut_and_skip_a_first_request(self):
        self.log(*self.sawtooth(), ace_line(self.S2, 10, "fold", 5000, 100))  # a fold that opens a session cuts nothing
        a = self.open().ace()["all"]
        self.assertEqual((a["requests"], a["sessions"], a["folds"]), (8, 2, 2))
        self.assertEqual(a["bins"], [0, 0, 1, 1, 0])  # the cuts of 10% (10 to 20%) and 29% (20 to 40%)
        self.assertEqual(a["acts"], {"pass": 3, "fold": 3, "keep": 2})
        self.assertEqual([r["sess"] for r in a["rows"]], [self.S2, self.S1])  # the newest first
        self.assertEqual(a["rows"][1]["folds"], 2)

    def test_the_older_log_file_comes_first_and_a_damaged_or_unfinished_line_is_skipped(self):
        self.log(ace_line(self.S1, 0, "pass", 100, 100), name="ace.log.1")
        self.log(ace_line(self.S1, 1, "pass", 200, 200), "garbage\n", "2026-01-02T03:02:00 pid=7 sess=%s act=pass in=x out=1\n" % self.S1,
                 ace_line(self.S1, 3, "pass", 300, 300)[:-1])  # no line end: not a whole line
        pts = self.open().ace()["points"]
        self.assertEqual([p["sent"] for p in pts], [100, 200])

    def test_no_log_gives_an_empty_answer_and_a_view_writes_nothing(self):
        before = sorted(os.listdir(self.root))
        d = self.open().ace()
        self.assertEqual((d["session"], d["points"], d["candles"], d["mix"]), ("", [], [], None))
        self.assertEqual(d["all"]["requests"], 0)
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_the_route_answers_over_http_with_the_token(self):
        self.log(*self.sawtooth())
        server = view.make_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/api/ace?per=2&k=%s" % (server.server_address[1], view.token(self.root))
        with urllib.request.urlopen(url) as r:
            d = json.load(r)
        self.assertEqual((len(d["points"]), len(d["candles"])), (7, 4))


class ApiTest(unittest.TestCase):
    def test_the_api_table_keeps_its_routes(self):
        """hermes-spacegraph's routes call view.API, so its names are a public contract (a new route may join)."""
        self.assertEqual(sorted(view.API), ["/api/ace", "/api/context", "/api/crumb", "/api/find", "/api/graph",
                                            "/api/path", "/api/session", "/api/trail", "/api/venn"])

if __name__ == "__main__":
    unittest.main()
