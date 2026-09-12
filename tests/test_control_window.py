"""Check control-window safety and real Windows widget construction."""
import platform
import unittest
from unittest.mock import Mock, patch

from main import OverlayApp, UI_ACCENT, UI_BG, UI_BUTTON, UI_PRIMARY, UI_SURFACE


class ControlWindowTest(unittest.TestCase):
    def app(self):
        app = OverlayApp((0, 0, 100, 100))
        app.root = Mock()
        app.root.focus_displayof.return_value = None
        app.overlay = Mock()
        app._start_button = Mock()
        return app

    def test_wave_uses_control_window_palette(self):
        for phase, color in ((0, UI_PRIMARY), (1 / 3, UI_ACCENT),
                             (2 / 3, UI_BUTTON), (1, UI_PRIMARY), (-1, UI_PRIMARY)):
            self.assertEqual(OverlayApp._wave_color(phase), color.lower())
        self.assertEqual(OverlayApp._wave_color(1 / 6), "#cc88e7")
        self.assertEqual(OverlayApp._wave_color(1 - 1e-8), UI_PRIMARY.lower())

    def test_detection_turns_wave_red_and_freezes_until_restart(self):
        app = self.app()
        app._canvas = Mock()
        app._rect_id = "rectangle"
        app._wave_ids = list(range(64))
        app._handle_ids = {corner: corner for corner in ("nw", "ne", "se", "sw")}

        def render_at(seconds):
            app._canvas.reset_mock()
            with patch("main.time.monotonic", return_value=seconds):
                app._render()
            return [next(iter(call.kwargs.values()))
                    for call in app._canvas.itemconfig.call_args_list]

        normal = render_at(2)
        app.hit = True
        stopped = render_at(3)
        self.assertEqual(len(stopped), 69)
        self.assertNotEqual(stopped, normal)
        for color in stopped:
            red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            self.assertGreater(red, green)
            self.assertEqual(green, blue)
        self.assertEqual(render_at(20), stopped)
        self.assertEqual(app._wave_phase, 0.25)
        app._toggle_enter_spam()
        resumed = render_at(22)
        self.assertFalse(app.hit)
        self.assertNotEqual(resumed, stopped)
        self.assertNotEqual(render_at(23), resumed)
        self.assertEqual(app.region, [0, 0, 100, 100])

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

    def test_repeated_f9_does_not_start_input_in_control_window(self):
        app = self.app()
        app.root.focus_displayof.return_value = app._start_button
        with patch.object(app, "_lock_mouse") as lock:
            for _ in range(10):
                app._toggle_enter_spam()
        self.assertFalse(app.enter_on)
        self.assertTrue(app._running)
        lock.assert_not_called()
        app.root.destroy.assert_not_called()
        self.assertIn("Focus the game", app.status_text)

    def test_repeated_f9_starts_and_stops_outside_app(self):
        app = self.app()
        with patch.object(app, "_lock_mouse"), patch.object(app, "_unlock_mouse"):
            for _ in range(5):
                app._toggle_enter_spam()
                self.assertTrue(app.enter_on)
                app._toggle_enter_spam()
                self.assertFalse(app.enter_on)
        self.assertTrue(app._running)
        self.assertEqual(app.status_text, "paused")
        app.root.destroy.assert_not_called()

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
            self.assertEqual(root.resizable(), (0, 0))
            self.assertEqual(root.cget("bg"), UI_BG)
            self.assertEqual(app._logo_image.width(), 144)
            self.assertEqual(app._logo_image.height(), 144)
            self.assertEqual(app._icon_image.width(), 256)
            self.assertEqual(app._activity_list.cget("bg"), UI_SURFACE)
            self.assertEqual(app._start_button.cget("bg"), UI_PRIMARY)
            self.assertEqual(app._record_button.cget("bg"), UI_BUTTON)
            self.assertEqual(app._start_button.cget("text"), "Start watching  (F9)")
            self.assertEqual(app._status_label.cget("anchor"), "center")
            self.assertEqual(app._detail_label.cget("justify"), "center")
            self.assertNotIn("center", app._activity_list.tag_names("1.0"))
            for row in (app._start_button.master, app._record_button.master):
                self.assertAlmostEqual(row.winfo_x() + row.winfo_width() / 2,
                                       row.master.winfo_width() / 2, delta=1)
            for button in (app._start_button, app._record_button):
                for sibling in button.master.winfo_children():
                    if sibling.winfo_class() == "Button":
                        self.assertNotEqual(sibling.cget("bg"), sibling.master.cget("bg"))
            mute = app._mute_button
            self.assertEqual(mute.winfo_class(), "Checkbutton")
            self.assertFalse(int(mute.cget("indicatoron")))
            self.assertEqual(mute.cget("image"), str(app._mute_images[0]))
            self.assertEqual(mute.cget("selectimage"), str(app._mute_images[1]))
            self.assertEqual(int(root.getvar(mute.cget("variable"))), 0)
            mute.invoke()
            app._render()
            self.assertFalse(app.beep_enabled)
            self.assertEqual(int(root.getvar(mute.cget("variable"))), 1)
            app._toggle_beep()  # F10 uses the same callback.
            app._render()
            self.assertTrue(app.beep_enabled)
            self.assertEqual(int(root.getvar(mute.cget("variable"))), 0)
            self.assertEqual(app._activity_list.cget("state"), "disabled")
            self.assertTrue(app.overlay.winfo_exists())
            self.assertEqual(app.region, [0, 0, 100, 100])
            self.assertEqual(int(app._canvas.cget("width")), 124)
            self.assertEqual(int(app._canvas.cget("height")), 124)
            # Check the wider border and the visible part of each corner handle.
            for x, y in ((10, 62), (114, 62), (62, 10), (62, 114)):
                self.assertIn(app._rect_id, app._canvas.find_overlapping(x, y, x, y))
            for corner, (x, y) in {"nw": (18, 18), "ne": (106, 18),
                                   "sw": (18, 106), "se": (106, 106)}.items():
                self.assertEqual(app._canvas.find_overlapping(x, y, x, y)[-1],
                                 app._handle_ids[corner])
            self.assertEqual(app._canvas.itemcget(app._rect_id, "fill"), "black")
            self.assertEqual(app.overlay.attributes("-transparentcolor"), "black")
            self.assertFalse(app._canvas.tag_bind("border", "<Double-Button-1>"))
            with patch("main.time.monotonic", return_value=0):
                app._render()
            wave = [app._canvas.itemcget(item, "fill") for item in app._wave_ids]
            self.assertEqual(len(set(wave)), 64)
            self.assertEqual(wave[0], UI_PRIMARY.lower())
            for corner, index in {"nw": 0, "ne": 16, "se": 32, "sw": 48}.items():
                fill = app._canvas.itemcget(app._handle_ids[corner], "fill")
                self.assertEqual(fill, wave[(index + 32) % 64])
                self.assertNotEqual(fill, wave[index])
            positions = [app._canvas.coords(item) for item in app._wave_ids]
            with patch("main.time.monotonic", return_value=2):
                app._render()
            self.assertEqual(app._canvas.itemcget(app._wave_ids[16], "fill"), wave[0])
            self.assertNotEqual(app._canvas.itemcget(app._wave_ids[0], "fill"), wave[0])
            self.assertEqual([app._canvas.coords(item) for item in app._wave_ids], positions)
            for item in app._wave_ids:
                self.assertIn("border", app._canvas.gettags(item))
            self.assertEqual(app.region, [0, 0, 100, 100])
        finally:
            if app.root is not None:
                app.root.destroy()


if __name__ == "__main__":
    unittest.main()
