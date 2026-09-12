"""Check region selection without global hooks or screen capture."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from main import OverlayApp, RegionSelector


def event(x, y):
    return SimpleNamespace(x=x, y=y)


class RegionSelectionTest(unittest.TestCase):
    def selector(self):
        selector = RegionSelector.__new__(RegionSelector)
        selector.root = Mock()
        selector.canvas = Mock()
        selector.canvas.winfo_width.return_value = 1920
        selector.canvas.winfo_height.return_value = 1080
        selector._info_font = None
        selector.left, selector.top = -1920, -200
        selector.result = None
        selector.on_done = Mock()
        selector.reset()
        return selector

    def test_drag_in_each_direction(self):
        for start, end in [((100, 100), (300, 200)), ((300, 200), (100, 100)),
                           ((100, 200), (300, 100)), ((300, 100), (100, 200))]:
            with self.subTest(start=start):
                selector = self.selector()
                selector._on_press(event(*start))
                selector._on_release(event(*end))
                self.assertEqual(selector.result, (-1820, -100, 200, 100))
                selector.on_done.assert_called_once_with(selector.result)

    def test_small_drag_reset_and_cancel(self):
        selector = self.selector()
        selector._on_press(event(10, 10))
        selector._on_release(event(12, 12))
        self.assertIsNone(selector.result)
        selector.root.destroy.assert_not_called()
        selector._on_press(event(10, 10))
        selector._on_drag(event(200, 200))
        selector.reset()
        selector._on_release(event(200, 200))
        selector.on_done.assert_not_called()
        selector._quit()
        selector.on_done.assert_called_once_with(None)

    def test_reselection_stops_spam_and_reuses_selector(self):
        app = OverlayApp((0, 0, 100, 100))
        app.root, app._panel = Mock(), Mock()
        app.enter_on = True
        with patch("main.RegionSelector") as selector, \
             patch.object(app, "_unlock_mouse") as unlock, \
             patch.object(app, "_sync_geometry"), patch("main.save_region") as save:
            app._start_selection()
            self.assertTrue(app._selecting)
            self.assertFalse(app.enter_on)
            unlock.assert_called()
            app._set_enter_on(True)
            self.assertFalse(app.enter_on)
            app._start_selection()
            self.assertEqual(selector.call_count, 1)
            selector.return_value.reset.assert_called_once()
            app._finish_selection((-100, 20, 50, 60))
            self.assertEqual(app.region, [-100, 20, 50, 60])
            save.assert_called_once_with((-100, 20, 50, 60))
            self.assertFalse(app._selecting)
            app._start_selection()
            app._finish_selection(None)
            self.assertEqual(app.region, [-100, 20, 50, 60])
            self.assertEqual(save.call_count, 1)

    def test_inflight_ocr_is_discarded(self):
        for select_on_read in (1, 2):
            app = OverlayApp((0, 0, 100, 100))
            app.enter_on = True
            calls = []

            def read(_sct):
                calls.append(1)
                if len(calls) == select_on_read:
                    app._region_revision += 1
                    app._running = False
                return "+123"

            with patch("main.mss.MSS"), \
                 patch.object(app, "_read_with_retry", side_effect=read), \
                 patch("main.threading.Thread") as thread:
                app._ocr_loop()
            thread.assert_not_called()
            self.assertIsNone(app.last_value)


if __name__ == "__main__":
    unittest.main()
