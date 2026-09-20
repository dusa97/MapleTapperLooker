"""Check control-window safety and real Windows widget construction."""
import platform
import unittest
from unittest.mock import Mock, patch

from main import OverlayApp, UI_ACCENT, UI_BG, UI_BUTTON, UI_PRIMARY, UI_SURFACE


class ControlWindowTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("discord_alerts.DiscordSettingsStore.load", return_value=None))

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

    def test_watching_binds_one_foreground_target_for_the_activation(self):
        app = self.app()
        target = Mock()
        with patch("main.bind_foreground_target", return_value=target) as bind, \
             patch.object(app, "_lock_mouse"):
            app._set_enter_on(True)
            app._set_enter_on(False)
            target.close.assert_not_called()
            bind.assert_called_once()
            app._set_enter_on(True)
            target.close.assert_called_once()
            self.assertEqual(bind.call_count, 2)

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
            self.assertEqual(app._read_with_retry(Mock()), (None, None))
        self.assertFalse(app.enter_on)
        self.assertIn("OCR error", app.status_text)

    @unittest.skipUnless(platform.system() == "Windows", "Windows overlay uses transparentcolor")
    def test_discord_settings_dialog_masks_webhook_and_cancel_keeps_settings(self):
        app = OverlayApp((0, 0, 100, 100), enter_spam=False)
        try:
            root = app._build_window()
            app._open_discord_settings()
            dialog = next(window for window in root.winfo_children()
                          if window.winfo_class() == "Toplevel" and window.title() == "Discord settings")
            entries = []
            buttons = []

            def collect(widget):
                for child in widget.winfo_children():
                    if child.winfo_class() == "Entry":
                        entries.append(child)
                    if child.winfo_class() == "Button":
                        buttons.append(child)
                    collect(child)
            collect(dialog)
            self.assertEqual(entries[0].cget("show"), "•")
            self.assertCountEqual([button.cget("text") for button in buttons],
                                  ["Save", "Cancel", "Clear settings", "Send test alert"])
            next(button for button in buttons if button.cget("text") == "Cancel").invoke()
            self.assertIsNone(app.discord.settings)
        finally:
            if app.root is not None:
                for pending in app.root.tk.call("after", "info"):
                    app.root.after_cancel(pending)
                app.root.destroy()

    @unittest.skipUnless(platform.system() == "Windows", "Windows overlay uses transparentcolor")
    def test_discord_checkbox_reflects_saved_toggle_and_clear_immediately(self):
        from discord_alerts import DiscordNotifier, DiscordSettings
        from tests.test_discord_alerts import MemoryStore
        app = OverlayApp((0, 0, 100, 100), enter_spam=False)
        saved = DiscordSettings("https://discord.com/api/webhooks/1/synthetic", "2", True)
        app.discord = DiscordNotifier(MemoryStore(saved))
        try:
            root = app._build_window()
            self.assertTrue(app._discord_var.get())
            app._discord_button.invoke()
            self.assertFalse(app.discord.enabled)
            self.assertFalse(app._discord_var.get())
            app._discord_button.invoke()
            self.assertTrue(app._discord_var.get())
            app._open_discord_settings()
            dialog = next(window for window in root.winfo_children()
                          if window.winfo_class() == "Toplevel" and window.title() == "Discord settings")
            def clear_button(widget):
                for child in widget.winfo_children():
                    if child.winfo_class() == "Button" and child.cget("text") == "Clear settings":
                        return child
                    found = clear_button(child)
                    if found is not None:
                        return found
            clear_button(dialog).invoke()
            self.assertFalse(app.discord.enabled)
            self.assertFalse(app._discord_var.get())
        finally:
            app.discord.close()
            if app.root is not None:
                for pending in app.root.tk.call("after", "info"):
                    app.root.after_cancel(pending)
                app.root.destroy()

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
            self.assertIsNone(app._activity_list)        # activity lives in the shell's Log tab
            self.assertEqual(app._start_button.cget("bg"), UI_PRIMARY)
            self.assertEqual(app._record_button.cget("bg"), UI_BUTTON)
            self.assertEqual(app._start_button.cget("text"), "Start watching  (F9)")
            self.assertEqual(app._status_label.cget("anchor"), "center")
            self.assertEqual(app._detail_label.cget("justify"), "center")
            for row in (app._record_button.master,):
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
            discord = app._discord_button
            self.assertEqual(discord.winfo_class(), "Checkbutton")
            self.assertFalse(int(discord.cget("indicatoron")))
            self.assertTrue(discord.cget("takefocus"))
            self.assertEqual(app._discord_settings_button.cget("text"), "Discord settings")
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
            # str(): newer Tk returns a color object here rather than the plain string
            self.assertEqual(str(app.overlay.attributes("-transparentcolor")), "black")
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
                for pending in app.root.tk.call("after", "info"):
                    app.root.after_cancel(pending)
                app.root.destroy()


if __name__ == "__main__":
    unittest.main()
