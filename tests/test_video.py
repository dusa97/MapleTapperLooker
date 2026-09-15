import platform
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from main import OverlayApp


@unittest.skipUnless(platform.system() == "Windows", "The control window requires Windows")
class VideoTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.patch_base = patch("main._BASE_DIR", self.base)
        self.patch_base.start()
        self.addCleanup(self.patch_base.stop)
        self.app = OverlayApp((0, 0, 100, 100), enter_spam=False)
        self.app._build_window()
        self.app.root.update()
        self.addCleanup(self.app.root.destroy)
        self.addCleanup(self.app._close_video)
        self.size = (self.app.root.winfo_width(), self.app.root.winfo_height())
        self.player = Mock()
        self.player.get_frame.return_value = (None, 0.01)
        self.factory = patch("ffpyplayer.player.MediaPlayer", return_value=self.player).start()
        self.addCleanup(patch.stopall)

    def open_video(self):
        directory = self.base / "videos"
        directory.mkdir()
        path = directory / "Мичманы.MP4"
        path.touch()
        (directory / "notes.txt").touch()
        (directory / "folder.mp4").mkdir()
        self.app._open_video()
        return path

    def test_executable_uses_bundled_videos_unless_external_folder_exists(self):
        bundle = self.base / "_MEI_bundle"
        (bundle / "videos").mkdir(parents=True)
        bundled_video = bundle / "videos" / "included.mp4"
        bundled_video.touch()
        with patch("main.sys.frozen", True, create=True), \
                patch("main.__file__", str(bundle / "main.py")):
            self.app._open_video()
            self.assertEqual(self.factory.call_args.args, (str(bundled_video),))
            self.app._close_video()
            external_video = self.open_video()
            self.assertEqual(self.factory.call_args.args, (str(external_video),))

    def test_missing_and_empty_directory(self):
        self.app._open_video()
        (self.base / "videos").mkdir()
        self.app._open_video()
        self.factory.assert_not_called()
        self.assertIsNone(self.app._video_panel)
        messages = []
        while not self.app._activity_pending.empty():
            messages.append(self.app._activity_pending.get())
        self.assertTrue(any("Cannot play video" in message for message in messages))
        self.assertTrue(any("No videos found" in message for message in messages))

    def test_attached_panel_sound_and_single_player(self):
        # Set the limit explicitly; the CI desktop can be narrower than this window.
        self.app.root.maxsize(self.size[0] + 480, self.size[1])
        path = self.open_video()
        self.app.root.update_idletasks()
        self.assertEqual(self.factory.call_args.args, (str(path),))
        self.assertEqual(self.factory.call_args.kwargs["ff_opts"]["volume"], 1.0)
        self.assertEqual(self.app.root.winfo_width(), self.size[0] + 480)
        self.assertEqual(self.app._video_panel.master, self.app.root)
        self.assertEqual(self.app._bored_button.cget("state"), "disabled")
        self.app._open_video()
        self.factory.assert_called_once()
        self.app._close_video()
        self.app.root.update_idletasks()
        self.player.close_player.assert_called_once()
        self.assertEqual(self.app.root.winfo_width(), self.size[0])
        self.assertEqual(self.app._bored_button.cget("state"), "normal")
        self.assertIsNone(self.app._video_after)

    def test_frames_fit_panel_without_cropping_or_stretching(self):
        self.open_video()
        self.app.root.update_idletasks()
        width = self.app._video_label.winfo_width()
        height = self.app._video_label.winfo_height()
        for size in ((256, 144), (144, 256), (1920, 1080)):
            with self.subTest(size=size):
                frame = Mock()
                frame.get_size.return_value = size
                frame.to_bytearray.return_value = [bytes(size[0] * size[1] * 3)]
                self.player.get_frame.return_value = ((frame, 0), 0.03)
                self.app.root.after_cancel(self.app._video_after)
                self.app._video_tick()
                image = self.app._video_image
                self.assertLessEqual(image.width(), width)
                self.assertLessEqual(image.height(), height)
                self.assertTrue(image.width() == width or image.height() == height)
                self.assertAlmostEqual(image.width() / image.height(), size[0] / size[1], delta=0.01)

    def test_eof_closes_panel(self):
        self.player.get_frame.return_value = (None, "eof")
        self.open_video()
        self.assertIsNone(self.app._video_panel)
        self.player.close_player.assert_called_once()

    def test_decoder_error_closes_panel(self):
        self.player.get_frame.side_effect = RuntimeError("Bad video")
        self.open_video()
        self.assertIsNone(self.app._video_panel)
        self.player.close_player.assert_called_once()

    def test_decode_thread_error_closes_panel(self):
        self.open_video()
        callback = self.factory.call_args.kwargs["callback"]
        callback("read:error", "Cannot decode")
        self.app.root.after_cancel(self.app._video_after)
        self.app._video_tick()
        self.assertIsNone(self.app._video_panel)
        self.player.close_player.assert_called_once()


if __name__ == "__main__":
    unittest.main()
