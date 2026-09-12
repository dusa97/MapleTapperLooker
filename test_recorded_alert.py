"""Check recording replacement and playback without microphone access."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from recorded_alert import RecordedAlert


class RecordedAlertTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "message.wav"
        self.alert = RecordedAlert(self.path)
        self.sound = Mock(SND_FILENAME=1, SND_ASYNC=2, SND_NODEFAULT=4)
        patches = [patch.dict("sys.modules", winsound=self.sound),
                   patch("recorded_alert.platform.system", return_value="Windows")]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    def save_audio(self, command):
        if command.startswith("save "):
            with wave.open(str(self.path.with_suffix(".tmp.wav")), "wb") as audio:
                audio.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
                audio.writeframes(b"\x01\x00" * 100)

    def test_record_stop_replace_and_play(self):
        fallback = Mock()
        self.alert.play(fallback)
        fallback.assert_called_once()
        fallback.reset_mock()
        self.path.write_bytes(b"previous message")
        with patch.object(self.alert, "_command", side_effect=self.save_audio) as command:
            self.alert.toggle()
            self.assertTrue(self.alert.recording)
            self.assertEqual(self.path.read_bytes(), b"previous message")
            self.alert.play(fallback)
            fallback.assert_not_called()
            self.alert.toggle()
            self.assertFalse(self.alert.recording)
            self.assertTrue(self.path.read_bytes().startswith(b"RIFF"))
            self.alert.play(fallback)
            self.sound.PlaySound.assert_called_with(str(self.path), 7)
            self.alert.toggle()
            self.assertTrue(self.alert.recording)
            self.alert.toggle()
            self.assertEqual(command.call_args_list.count(
                unittest.mock.call("record maple_message")), 2)
        self.assertFalse(self.path.with_suffix(".tmp.wav").exists())
        # A fresh application instance uses the saved message.
        RecordedAlert(self.path).play(fallback)
        fallback.assert_not_called()

    def test_failures_keep_previous_message_and_allow_retry(self):
        for failure in ("open", "record", "stop", "save", "close"):
            with self.subTest(failure=failure):
                self.path.write_bytes(b"previous message")
                self.alert.recording = failure in ("stop", "save", "close")

                def fail(command):
                    if command.startswith(failure + " "):
                        raise RuntimeError("Microphone unavailable")
                    self.save_audio(command)

                with patch.object(self.alert, "_command", side_effect=fail):
                    self.alert.toggle()
                self.assertFalse(self.alert.recording)
                self.assertEqual(self.path.read_bytes(), b"previous message")
                with patch.object(self.alert, "_command"):
                    self.alert.toggle()
                    self.assertTrue(self.alert.recording)
                    self.alert.recording = False

    def test_invalid_recording_keeps_previous_message(self):
        self.path.write_bytes(b"previous message")
        self.alert.recording = True
        with patch.object(self.alert, "_command"):
            self.alert.toggle()
        self.assertEqual(self.path.read_bytes(), b"previous message")
        self.assertFalse(self.alert.recording)

    def test_playback_failure_and_shutdown(self):
        fallback = Mock()
        self.path.write_bytes(b"audio")
        self.sound.PlaySound.side_effect = RuntimeError("No output device")
        self.alert.play(fallback)
        fallback.assert_called_once()
        self.sound.PlaySound.side_effect = None
        self.alert.recording = True
        with patch.object(self.alert, "_command") as command:
            self.alert.close()
            command.assert_called_once_with("close maple_message")
            self.alert.toggle()
            self.alert.play(fallback)
        self.assertFalse(self.alert.recording)
        self.assertEqual(fallback.call_count, 1)


if __name__ == "__main__":
    unittest.main()
