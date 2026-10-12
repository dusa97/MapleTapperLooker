"""A roll skipped on its tier icons must still hand back those icons.

With a goal that asks for legendary lines, most rolls are ruled out from the
icon colours alone and never read. The icons are what the display then shows:
without them it keeps the previous roll's text, and a run that is working
normally looks frozen.
"""
import unittest
from types import SimpleNamespace

import numpy as np
from PIL import Image

from main import CUBE_PROFILES, CubesApp, POTENTIAL_TIER_COLOURS


def panel(tiers):
    """A Bright-sized panel whose three tier icons are the given colours."""
    profile = CUBE_PROFILES["bright"]
    w, h = 202, 70
    a = np.zeros((h, w, 3), dtype="uint8")
    a[:, :] = (24, 48, 48)
    x0, x1 = (round(w * f) for f in profile.icon_x)
    for i, tier in enumerate(tiers):
        if tier is None:
            continue
        y0 = round(h * (profile.icon_y[0] + i * profile.line_step))
        y1 = round(h * (profile.icon_y[1] + i * profile.line_step))
        a[y0:y1, x0:x1] = Image.new("RGB", (1, 1), POTENTIAL_TIER_COLOURS[tier]).getpixel((0, 0))
    return Image.fromarray(a, "RGB")


def app(img):
    return SimpleNamespace(region=(0, 0, img.width, img.height),
                           panels=[(0, 0, img.width, img.height)],
                           profile=CUBE_PROFILES["bright"])


class SkipShowsTiersTest(unittest.TestCase):
    def test_two_legendary_lines_are_not_skipped(self):
        img = panel(["legendary", "legendary", "unique"])
        could, seen = CubesApp._tiers_could_match(app(img), img, 2)
        self.assertTrue(could)
        self.assertEqual(seen, [["legendary", "legendary", "unique"]])

    def test_one_legendary_line_is_skipped_but_still_reports_its_tiers(self):
        img = panel(["legendary", "unique", "unique"])
        could, seen = CubesApp._tiers_could_match(app(img), img, 2)
        self.assertFalse(could)
        self.assertEqual(seen, [["legendary", "unique", "unique"]],
                         "a skipped roll must still say what the icons were")


if __name__ == "__main__":
    unittest.main()
