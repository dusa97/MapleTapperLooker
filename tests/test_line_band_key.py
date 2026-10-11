"""A stat line must hash to the same template key however bright it is drawn.

The game draws a freshly rolled card dimmer for about half a second, and
another player's monitor shifts every stroke the same way. When the key was
taken from raw brightness, each of those produced a different key: the table
filled with one entry per brightness and the hit rate collapsed - a real run
grew the table from 167 to 235 entries while its known rate fell 22% to 2%.
"""
import unittest

import numpy as np
from PIL import Image

from main import CUBE_PROFILES, line_band_key


def panel(brightness=1.0):
    """A Glowing-sized panel with three light-grey lines on a dark teal card."""
    profile = CUBE_PROFILES["glowing"]
    a = np.zeros((70, 250, 3), dtype=float)
    a[:, :] = (24, 48, 48)                       # the card
    for line in range(3):
        y = round(70 * (profile.icon_y[0] + line * profile.line_step)) + 3
        for n, x in enumerate(range(60, 200, 9)):  # strokes, some thin and dim
            a[y:y + 6, x:x + 2 + n % 2] = 210 if n % 3 else 150
    return Image.fromarray(np.clip(a * brightness, 0, 255).astype("uint8"), "RGB")


class LineBandKeyTest(unittest.TestCase):
    def test_key_survives_the_dim_redraw(self):
        profile = CUBE_PROFILES["glowing"]
        want = [line_band_key(panel(), profile, i)[0] for i in range(3)]
        self.assertTrue(all(want), "the sample panel must produce three keys")
        for brightness in (0.92, 0.85, 0.78, 1.08):
            got = [line_band_key(panel(brightness), profile, i)[0] for i in range(3)]
            self.assertEqual(want, got, f"key changed at brightness x{brightness}")

    def test_different_text_still_gets_a_different_key(self):
        profile = CUBE_PROFILES["glowing"]
        other = panel()
        other.paste(Image.new("RGB", (40, 8), (210, 210, 210)), (100, 20))
        self.assertNotEqual(line_band_key(panel(), profile, 0)[0],
                            line_band_key(other, profile, 0)[0])


if __name__ == "__main__":
    unittest.main()
