"""The icon that Hermes gives to the harness table: the viewers draw it beside the brand."""
import base64
import struct
import unittest

from breadcrumbs import ports
from breadcrumbs_hermes import workdir


class HermesIcon(unittest.TestCase):
    def entry(self):
        ports.load()
        return ports.harnesses()["hermes"]

    def test_hermes_gives_a_small_png_icon_and_workdir_names_the_same_key(self):
        entry = self.entry()
        self.assertEqual((entry["label"], workdir.HARNESS), ("Hermes", "hermes"))
        head, _, body = entry["icon"].partition(",")
        self.assertEqual(head, "data:image/png;base64")
        png = base64.b64decode(body)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (96, 96))  # sharp at 24 px on a screen of 3x
        self.assertLess(len(png), 20_000, "it travels in the answer of a route, so it stays small")


if __name__ == "__main__":
    unittest.main()
