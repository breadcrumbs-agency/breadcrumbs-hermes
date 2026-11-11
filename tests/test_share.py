import json
import os
import shutil
import subprocess
import tempfile
import unittest

from breadcrumbs.crumbs import Crumb, get_crumb, put_crumb
from breadcrumbs.index import Index
from breadcrumbs.lines import put_text
from breadcrumbs.share import DAG_JSON, RAW, cid_bytes, cid_str, export_car, import_car, publish, read_car
from breadcrumbs.store import Store
from breadcrumbs.zoom import Zoom

HELLO_RAW = "bafkreicysg23kiwv34eg2d7qweipxwosdo2py4ldv42nbauguluen5v6am"  # kubo 0.43.1: printf 'hello\n' | ipfs block put --cid-codec raw
KUBO_NODE = b'{"ab":2,"b":1,"ranges":[],"title":"t","zz":{"/":"' + HELLO_RAW.encode() + b'"}}'
KUBO_NODE_CID = "baguqeerao4c7ufk7njw72q5i3xxwvvjhmlkktafrtw4jmgvhow6oombcdloa"  # kubo 0.43.1: ipfs dag put (dag-json)


class CidTest(unittest.TestCase):
    def test_raw_cid_matches_kubo(self):
        self.assertEqual(cid_str(cid_bytes(RAW, b"hello\n")), HELLO_RAW)

    def test_dag_json_cid_matches_kubo(self):
        self.assertEqual(cid_str(cid_bytes(DAG_JSON, KUBO_NODE)), KUBO_NODE_CID)


class ShareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "a"))
        doc = put_text(self.store, b"public line one\npublic line two\nSECRET private line\n")
        self.priv = put_crumb(self.store, Crumb("turn", "private chatter", ranges=[[doc, 2, 3]]))
        self.pub = put_crumb(self.store, Crumb("turn", "shared fact", ranges=[[doc, 0, 2]], public=True,
                                               meta={"role": "user", "ts": 5, "sess": "SESSION-SECRET"}))
        self.root = put_crumb(self.store, Crumb("sum", "summary", kids=[self.pub, self.priv],
                                                heritage=[self.priv], public=True))
        self.car = os.path.join(self.tmp.name, "out.car")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_private_crumbs_lines_and_meta_never_leave(self):
        roots, stats = export_car(self.store, [self.root], self.car)
        with open(self.car, "rb") as f:
            data = f.read()
        self.assertEqual(len(roots), 1)
        self.assertNotIn(b"SECRET", data)
        self.assertNotIn(self.priv.encode(), data)
        self.assertEqual(stats["withheld"], 1)
        self.assertIn(b"public line one", data)

    def test_a_private_root_exports_nothing(self):
        with self.assertRaises(ValueError):
            export_car(self.store, [self.priv], self.car)
        self.assertFalse(os.path.exists(self.car))

    def test_every_block_verifies_and_the_root_is_listed(self):
        roots, _ = export_car(self.store, [self.root], self.car)
        got_roots, blocks = read_car(self.car)
        self.assertEqual(got_roots, roots)
        self.assertIn(roots[0], blocks)
        node = json.loads(blocks[roots[0]])
        self.assertEqual(node["kind"], "sum")
        self.assertEqual(len(node["kids"]), 1)  # the private kid is unlisted

    def test_a_tampered_block_is_rejected(self):
        export_car(self.store, [self.root], self.car)
        with open(self.car, "rb") as f:
            data = f.read().replace(b"public line one", b"public line 0ne")
        with open(self.car, "wb") as f:
            f.write(data)
        with self.assertRaises(ValueError):
            read_car(self.car)

    def test_roundtrip_into_a_fresh_store(self):
        roots, _ = export_car(self.store, [self.root], self.car)
        with Store(os.path.join(self.tmp.name, "b")) as other:
            index = Index(":memory:")
            local = import_car(other, index, self.car)
            text = Zoom(other).render(local[0], 1.0)
            self.assertIn("public line one", text)
            self.assertIn("public line two", text)
            self.assertNotIn("SECRET", text)
            c = get_crumb(other, local[0])
            self.assertTrue(c.public)
            self.assertEqual(c.meta["cid"], roots[0])
            self.assertTrue(index.search("public line"))
            index.close()

    def test_level_1_shows_the_same_text_in_each_store_that_holds_the_crumb(self):
        """The dispatch handbook's rule for exact context. A level below 1 picks words by the word counts
        of the store that shows it, so two stores can show different words there. The share on 3 hosts
        (Nov 8 2026) showed the same bytes at levels 0 and 1, and different bytes at 0.05, 0.2 and 0.5."""
        export_car(self.store, [self.root], self.car)
        shown = []
        for name, other_words in (("b", 0), ("c", 40)):
            with Store(os.path.join(self.tmp.name, name)) as other:
                index = Index(":memory:")
                for i in range(other_words):
                    index.add("x%07d" % i, "dx%d:0-1" % i, "walrus walrus kettle shelf %d" % i)
                local = import_car(other, index, self.car)
                zoom = Zoom(other, summaries=index.summary_of, cues=index.signature, words=index.tiers)
                shown.append((local[0], zoom.render(local[0], 1.0)))
                index.close()
        self.assertEqual(shown[0], shown[1])
        self.assertIn("public line one", shown[0][1])

    def test_publish_makes_a_public_copy_and_keeps_the_original(self):
        pid = publish(self.store, self.priv)
        self.assertNotEqual(pid, self.priv)
        self.assertTrue(get_crumb(self.store, pid).public)
        self.assertFalse(get_crumb(self.store, self.priv).public)

    @unittest.skipUnless(shutil.which("ipfs"), "kubo (ipfs) is not installed")
    def test_kubo_imports_the_car_and_serves_the_same_bytes(self):
        roots, _ = export_car(self.store, [self.root], self.car)
        _, blocks = read_car(self.car)
        env = dict(os.environ, IPFS_PATH=os.path.join(self.tmp.name, "ipfs"))  # never the user's repo
        run = lambda *a, **k: subprocess.run(["ipfs", *a], env=env, capture_output=True, check=True, **k)
        run("init", "--profile", "test")
        run("dag", "import", "--pin-roots=false", self.car)
        for cid, data in blocks.items():
            self.assertEqual(run("block", "get", cid).stdout, data)
        self.assertEqual(json.loads(run("dag", "get", roots[0]).stdout), json.loads(blocks[roots[0]]))


if __name__ == "__main__":
    unittest.main()
