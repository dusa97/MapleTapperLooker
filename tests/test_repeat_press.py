"""A refused press brings the same panel back, and that is how it is spotted.

The game refuses a press for about 1.2s after a roll, then finishes the old
animation - which looks like a change. Nearly half of every Bright run was
this: one cube counted, read and displayed twice.

The first attempt compared whole grabs and caught 2 repeats out of 184,
because the region around the cards holds moving game background and never
compares equal. The comparison has to be the card TEXT, which is what these
check - with a margin between "same roll" and "new roll" wide enough that the
cut between them is not a coin toss.
"""
import unittest

import numpy as np
from PIL import Image

from main import CUBES_SAME_ROLL_INK, CubesApp


def panel(seed, shimmer=0, background=0):
    """A panel of text-like strokes. A different seed is a different roll;
    shimmer redraws a few stray pixels; background moves the area around the
    card, as the game's own animation does behind the dialog."""
    rng = np.random.default_rng(seed)
    a = np.zeros((70, 202, 3), dtype="uint8")
    a[:, :] = (24, 48, 48)
    for line in range(3):
        y = 8 + line * 24
        for x in range(30, 190, 7):
            if rng.random() < 0.6:
                a[y:y + 9, x:x + 3] = 220
    if shimmer:
        noise = np.random.default_rng(shimmer)
        for _ in range(12):
            y, x = noise.integers(0, 60), noise.integers(0, 190)
            a[y:y + 1, x:x + 1] = 220
    if background:
        bg = np.random.default_rng(background)
        a[:6, :] = bg.integers(0, 255, (6, 202, 3), dtype="uint8")
        a[-6:, :] = bg.integers(0, 255, (6, 202, 3), dtype="uint8")
    return Image.fromarray(a, "RGB")


def diff(a, b):
    x, y = CubesApp._ink(a), CubesApp._ink(b)
    return float((x != y).mean())


class RepeatPressTest(unittest.TestCase):
    def test_the_same_panel_reads_as_the_same_roll(self):
        self.assertLess(diff(panel(1), panel(1)), CUBES_SAME_ROLL_INK)

    def test_a_repeat_survives_shimmer_in_the_card(self):
        self.assertLess(diff(panel(1), panel(1, shimmer=7)), CUBES_SAME_ROLL_INK,
                        "a few stray pixels must not make a repeat look like a new roll")

    def test_a_new_roll_is_never_taken_for_a_repeat(self):
        for seed in range(2, 22):
            self.assertGreater(diff(panel(1), panel(seed)), CUBES_SAME_ROLL_INK,
                               f"roll {seed} read as a repeat of roll 1")

    def test_the_cut_sits_between_the_two_with_room_to_spare(self):
        """What went wrong last time was a cut on the wrong side of the data."""
        same = max(diff(panel(s), panel(s, shimmer=s + 99)) for s in range(1, 12))
        new = min(diff(panel(1), panel(s)) for s in range(2, 22))
        self.assertLess(same * 3, CUBES_SAME_ROLL_INK, f"repeats measure up to {same:.4f}")
        self.assertGreater(new, CUBES_SAME_ROLL_INK * 3, f"new rolls measure from {new:.4f}")


if __name__ == "__main__":
    unittest.main()
