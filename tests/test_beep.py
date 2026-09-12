"""Check detection beeps without screen capture or keyboard input."""
import unittest
from unittest.mock import Mock, patch

from main import BEEP_COOLDOWN, OverlayApp, beep


class DetectionBeepTest(unittest.TestCase):
    def test_alert_uses_recording_with_beep_fallback(self):
        app = OverlayApp((0, 0, 100, 100))
        with patch.object(app.alert, "play") as play:
            app._play_alert()
        play.assert_called_once_with(beep)

    def test_audio_hotkeys_without_spam_and_cleanup(self):
        app = OverlayApp((0, 0, 100, 100), enter_spam=False)
        with patch.object(app, "_build_window") as window, \
             patch("main.keyboard.add_hotkey") as register, \
             patch("main.threading.Thread"), \
             patch("main.keyboard.remove_hotkey") as remove, \
             patch.object(app.alert, "close") as close:
            app.root = window.return_value
            app.run()
            register.assert_any_call("f11", app._audio_requests.put, args=(app.alert.toggle,),
                                     suppress=True, trigger_on_release=True)
            register.assert_any_call("f12", app._audio_requests.put, args=(app.alert.reset,),
                                     suppress=True, trigger_on_release=True)
            # The hook queues work; the UI processes each key release once.
            with patch.object(app.alert, "toggle") as toggle, \
                 patch.object(app.alert, "reset") as reset:
                app.beep_enabled = False
                app._audio_requests.put(toggle)
                app._audio_requests.put(reset)
                toggle.assert_not_called()
                reset.assert_not_called()
                app._render()
                toggle.assert_called_once()
                reset.assert_not_called()
                app._render()
                app._render()
                reset.assert_called_once()
                self.assertFalse(app.beep_enabled)
            register.assert_any_call(app.beep_hotkey, app._toggle_beep)
            register.assert_any_call("f8", app._selection_requested.set,
                                     suppress=True, trigger_on_release=True)
            register.assert_any_call(app.enter_hotkey, app._toggle_requests.put, args=(True,),
                                     suppress=True, trigger_on_release=True)
            self.assertEqual(register.call_count, 5)
            app._quit()
            remove.assert_any_call("f8")
            remove.assert_any_call("f11")
            remove.assert_any_call("f12")
            remove.assert_any_call(app.beep_hotkey)
            close.assert_called_once()

    def test_detection_beep(self):
        cases = [
            # Spam, beep enabled, OCR reads, previous beep, expected beeps.
            (False, True, ["+123", "+123", "+123"], 0, 0),
            (True, True, ["+123", "+123"], 0, 1),
            (False, False, ["+123", "+123"], 0, 0),
            (True, True, ["+123", None], 0, 0),
            (True, True, ["+123", "+123"], 100, 0),
            (True, False, ["+123", "+123"], 0, 0),
        ]
        for spam, enabled, values, last_beep, expected in cases:
            with self.subTest(spam=spam, enabled=enabled, values=values,
                              last_beep=last_beep):
                app = OverlayApp((0, 0, 100, 100))
                app.enter_on = spam
                app.beep_enabled = enabled
                app.last_beep_time = last_beep
                reads = iter(values)

                def read(_sct):
                    value = next(reads, None)
                    if value is None:
                        app._running = False
                    return value

                with patch("main.mss.MSS"), \
                     patch.object(app, "_read_with_retry", side_effect=read) as ocr, \
                     patch("main.time.sleep", side_effect=lambda _: setattr(app, "_running", False)), \
                     patch.object(app, "_lock_mouse"), \
                     patch.object(app, "_unlock_mouse"), \
                     patch("main.time.perf_counter", return_value=100 + BEEP_COOLDOWN / 2), \
                     patch("main.threading.Thread") as thread:
                    app._ocr_loop()

                self.assertEqual(thread.call_count, expected)
                if expected:
                    thread.assert_called_once_with(target=app._play_alert, daemon=True)
                    thread.return_value.start.assert_called_once()
                if not spam:
                    ocr.assert_not_called()
                elif values[-1] is not None:
                    self.assertFalse(app.enter_on)
                else:
                    self.assertTrue(app.enter_on)

    def test_disable_during_read_discards_detection(self):
        for stop_at in (1, 2):
            with self.subTest(stop_at=stop_at):
                app = OverlayApp((0, 0, 100, 100))
                app.enter_on = True
                calls = 0

                def read(_sct):
                    nonlocal calls
                    calls += 1
                    if calls == stop_at:
                        app._toggle_enter_spam()
                        app._running = False
                    return "+123"

                with patch("main.mss.MSS"), \
                     patch.object(app, "_unlock_mouse"), \
                     patch.object(app, "_read_with_retry", side_effect=read), \
                     patch("main.threading.Thread") as thread:
                    app._ocr_loop()
                thread.assert_not_called()
                self.assertEqual(app.status_text, "paused")
                self.assertFalse(app.hit)

    def test_f9_restarts_after_detection(self):
        app = OverlayApp((0, 0, 100, 100))
        app.hit = app._prev_hit = True
        with patch.object(app, "_lock_mouse"):
            app._toggle_enter_spam()
        self.assertTrue(app.enter_on)
        self.assertFalse(app.hit)
        self.assertFalse(app._prev_hit)

    def test_window_start_waits_for_countdown_before_automation(self):
        app = OverlayApp((0, 0, 100, 100))
        app.root = Mock()
        app.root.after.return_value = "countdown"
        app.root.focus_displayof.return_value = None
        app._start_button = Mock()
        with patch.object(app, "_lock_mouse") as lock:
            app._start_or_stop()
            self.assertFalse(app.enter_on)
            self.assertIn("Starting in 3", app.status_text)
            app._finish_countdown(0)
        self.assertTrue(app.enter_on)
        lock.assert_called_once()

    def test_activity_is_bounded_in_mocked_window_render(self):
        app = OverlayApp((0, 0, 100, 100))
        app.root = Mock()
        app._activity_list = Mock()
        for index in range(8):
            app._log(f"event {index}")
        app._render()
        self.assertEqual(len(app._activity_lines), 6)
        self.assertEqual(app._activity_list.insert.call_count, 6)


if __name__ == "__main__":
    unittest.main()
