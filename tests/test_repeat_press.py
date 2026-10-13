"""A refused press brings the same panel back, and that is how it is spotted.

The game refuses a press for about 1.2s after a roll, then finishes the old
animation - which looks like a change. Nearly half of every Bright run was
this: one cube counted, read and displayed twice. The check that tells the two
apart is _same_frame over the settled grab, so it has to hold on real panels.
"""
import unittest

import numpy as np
from PIL import Image

from main import CUBE_PROFILES, CubesApp


def panel(seed, brightness=1.0):
    """A panel with text-like strokes; a different seed is a different roll."""
    rng = np.random.default_rng(seed)
    a = np.zeros((70, 202, 3), dtype=float)
    a[:, :] = (24, 48, 48)
    for line in range(3):
        y = 8 + line * 24
        for x in range(30, 190, 7):
            if rng.random() < 0.6:
                a[y:y + 9, x:x + 3] = 220
    return Image.fromarray(np.clip(a * brightness, 0, 255).astype("uint8"), "RGB")


class RepeatPressTest(unittest.TestCase):
    def test_the_same_panel_is_recognised_as_a_repeat(self):
        self.assertTrue(CubesApp._same_frame(panel(1), panel(1)))

    def test_a_new_roll_is_not_a_repeat(self):
        for seed in range(2, 12):
            self.assertFalse(CubesApp._same_frame(panel(1), panel(seed)),
                             f"roll {seed} read as a repeat of roll 1")

    def test_a_repeat_survives_the_dimmer_redraw(self):
        """The repeat may be caught part-way through the fade, so a little
        brightness drift must not read as a new roll."""
        self.assertTrue(CubesApp._same_frame(panel(1), panel(1, 0.95)))


if __name__ == "__main__":
    unittest.main()
