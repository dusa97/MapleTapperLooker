"""Cubes' 'Combat Power increase' goal: stops on a '+' like Flames does, and the
CP box under each Bright AFTER card reads through the Flames reader."""
import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main as m  # noqa: E402

LINES = [("LUK", "+13%"), ("LUK", "+13%"), ("All Stats", "+10%")]
TIERS = ["legendary"] * 3


class CPGoalTest(unittest.TestCase):

    def test_cp_goal_hits_only_on_a_plus(self):
        g = m.CPGoal()
        assert g.check(LINES, TIERS, "+1,242,495")
        assert not g.check(LINES, TIERS, None)
        assert not g.check(LINES, TIERS)                     # old 2-arg callers still work

    def test_cp_goal_inside_and_or(self):
        luk = m.ComboGoal(["LUK"], 2)
        both = m.CombinedGoal([luk, m.CPGoal()], ["all"])
        assert both.check(LINES, TIERS, "+5")
        assert not both.check(LINES, TIERS, None)            # AND: lines alone are not enough
        assert m._uses_cp(both) and m._uses_cp(m.LegendaryOnly(m.CPGoal(), 3))
        assert not m._uses_cp(luk)

    def test_bright_cp_boxes_read_like_flames(self):
        root = Path(m.__file__).parent
        img = Image.open(root / "assets/reference/bright_x3_example2.png").convert("RGB")
        label = np.array(Image.open(m.LABEL_TEMPLATE_PATH).convert("L"))
        _best, peaks, (sh, sw) = m.locate_label(np.array(img.convert("L")), label, max_peaks=4)
        reads = []
        for px, py in sorted(peaks):
            x, y, w, h = m.cp_number_box(px, py, sw, sh)
            reads.append(m.read_delta_from_image(img.crop((x, y, x + w, y + h)), save_debug=False))
        assert reads == [None, "+19,128,727", "+1,242,495", "+5,927,662"]   # BEFORE's 0 is no increase


if __name__ == "__main__":
    unittest.main()
