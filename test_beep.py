"""Check detection beeps without screen capture or keyboard input."""
import unittest
from unittest.mock import patch

from main import BEEP_COOLDOWN, OverlayApp, beep


class DetectionBeepTest(unittest.TestCase):
    def test_detection_beep(self):
        cases = [
            # Spam, beep enabled, OCR reads, previous beep, expected beeps.
            (False, True, ["+123", "+123", "+123"], 0, 1),
            (True, True, ["+123", "+123"], 0, 1),
            (False, False, ["+123", "+123"], 0, 0),
            (False, True, ["+123", None], 0, 0),
            (False, True, ["+123", "+123"], 100, 0),
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
                     patch.object(app, "_read_with_retry", side_effect=read), \
                     patch.object(app, "_unlock_mouse"), \
                     patch("main.time.perf_counter", return_value=100 + BEEP_COOLDOWN / 2), \
                     patch("main.threading.Thread") as thread:
                    app._ocr_loop()

                self.assertEqual(thread.call_count, expected)
                if expected:
                    thread.assert_called_once_with(target=beep, daemon=True)
                    thread.return_value.start.assert_called_once()
                if spam:
                    self.assertFalse(app.enter_on)


if __name__ == "__main__":
    unittest.main()
