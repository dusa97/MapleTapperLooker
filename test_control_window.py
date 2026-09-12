"""Check control-window safety and real Windows widget construction."""
import platform
import unittest
from unittest.mock import Mock, patch

from main import OverlayApp


class ControlWindowTest(unittest.TestCase):
    def app(self):
        app = OverlayApp((0, 0, 100, 100))
        app.root = Mock()
        app.root.focus_displayof.return_value = None
        app.overlay = Mock()
        app._start_button = Mock()
        return app

    def test_missing_ocr_blocks_start_and_f9(self):
        app = self.app()
        app._ocr_available = False
        with patch.object(app, "_lock_mouse") as lock:
            app._start_or_stop()
            app._toggle_enter_spam()
            app._set_enter_on(True)
        self.assertFalse(app.enter_on)
        app.root.after.assert_not_called()
        lock.assert_not_called()

    def test_region_selection_cancels_countdown(self):
        app = self.app()
        app._start_after = "pending-start"
        with patch("main.RegionSelector"), patch.object(app, "_unlock_mouse"):
            app._start_selection()
        app.root.after_cancel.assert_called_once_with("pending-start")
        self.assertIsNone(app._start_after)
        self.assertFalse(app.enter_on)

    def test_button_restarts_after_detection(self):
        app = self.app()
        app.hit = app._prev_hit = True
        with patch.object(app, "_lock_mouse"):
            app._start_or_stop()
            app._finish_countdown(0)
        self.assertTrue(app.enter_on)
        self.assertFalse(app.hit)
        self.assertFalse(app._prev_hit)

    def test_f9_queue_cancels_countdown_on_render(self):
        app = self.app()
        app._start_after = "pending-start"
        app._toggle_requests.put(True)
        app.root.after_cancel.assert_not_called()
        app._render()
        app.root.after_cancel.assert_called_once_with("pending-start")
        self.assertFalse(app.enter_on)

    def test_countdown_never_starts_input_in_control_window(self):
        app = self.app()
        app.root.focus_displayof.return_value = app._start_button
        with patch.object(app, "_lock_mouse") as lock:
            app._finish_countdown(0)
        self.assertFalse(app.enter_on)
        lock.assert_not_called()
        self.assertIn("Start cancelled", app.status_text)

    def test_ocr_error_stops_automation(self):
        app = self.app()
        app.enter_on = True
        with patch("main.read_delta", side_effect=RuntimeError("capture failed")), \
             patch.object(app, "_unlock_mouse"):
            self.assertIsNone(app._read_with_retry(Mock()))
        self.assertFalse(app.enter_on)
        self.assertIn("OCR error", app.status_text)

    @unittest.skipUnless(platform.system() == "Windows", "Windows overlay uses transparentcolor")
    def test_real_window_build_and_render(self):
        app = OverlayApp((0, 0, 100, 100), enter_spam=False)
        try:
            root = app._build_window()
            app._render()
            root.update_idletasks()
            self.assertGreaterEqual(root.winfo_width(), 540)
            self.assertEqual(app._start_button.cget("text"), "Start watching")
            self.assertEqual(app._activity_list.cget("state"), "disabled")
            self.assertTrue(app.overlay.winfo_exists())
        finally:
            if app.root is not None:
                app.root.destroy()


if __name__ == "__main__":
    unittest.main()
