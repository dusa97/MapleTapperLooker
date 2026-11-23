"""F7 searches only the MapleStory window, so a stream of someone else's game
showing the same dialog can't be picked; the whole desktop only as a fallback."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main as m  # noqa: E402

DESKTOP = {"left": 0, "top": 0, "width": 5120, "height": 1440}


class FakeSct:
    monitors = [DESKTOP]


class SearchAreaTest(unittest.TestCase):
    def area(self, game):
        with patch.object(m, "maple_window_rect", return_value=game):
            return m.search_area(FakeSct())

    def test_game_window_only(self):
        game = {"left": 241, "top": 53, "width": 1920, "height": 1090}
        self.assertEqual(self.area(game), (game, True))

    def test_clipped_to_desktop(self):
        area, in_game = self.area({"left": -100, "top": 1000, "width": 800, "height": 800})
        self.assertTrue(in_game)
        self.assertEqual(area, {"left": 0, "top": 1000, "width": 700, "height": 440})

    def test_no_game_falls_back_to_whole_desktop(self):
        self.assertEqual(self.area(None), (DESKTOP, False))
        self.assertEqual(self.area({"left": 9000, "top": 0, "width": 800, "height": 600}), (DESKTOP, False))


if __name__ == "__main__":
    unittest.main()
