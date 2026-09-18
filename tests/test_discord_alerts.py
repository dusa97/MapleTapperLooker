"""Behavior checks for Discord settings, delivery, and capture boundaries."""
import json
import os
import ctypes
import sys
from types import SimpleNamespace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from discord_alerts import (DiscordNotifier, DiscordSettings, DiscordSettingsStore,
                            WindowTarget, _Job, capture_entry, send_webhook, validate_settings)


class MemoryStore:
    supported = True

    def __init__(self, settings=None):
        self.settings = settings

    def load(self):
        return self.settings

    def save(self, webhook_url, user_id, enabled=False):
        self.settings = DiscordSettings(webhook_url, user_id, enabled)
        return self.settings

    def clear(self):
        self.settings = None


class DiscordSettingsTest(unittest.TestCase):
    def test_first_use_is_disabled_and_validation_happens_before_storage(self):
        store = DiscordSettingsStore("settings.bin", protect=Mock())
        self.assertIsNone(store.load())
        with self.assertRaises(ValueError):
            store.save("not-a-webhook", "1")
        store.protect.assert_not_called()

    def test_valid_settings_are_normalized(self):
        settings = validate_settings(
            "https://discord.com/api/v10/webhooks/1234567890/token_-.x", "42")
        self.assertEqual(settings.user_id, "42")
        self.assertEqual(settings.webhook_url,
                         "https://discord.com/api/v10/webhooks/1234567890/token_-.x")

    def test_rejects_noncanonical_urls_before_network(self):
        bad_urls = ("http://discord.com/api/webhooks/1/token", "https://evil.test/api/webhooks/1/token",
                    "https://discord.com:443/api/webhooks/1/token", "https://discord.com/api/webhooks/1/token?q=1",
                    "https://discord.com/api/v9/webhooks/1/token", "https://user@discord.com/api/webhooks/1/token")
        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_settings(url, "1")

    def test_save_writes_ciphertext_only_and_failure_keeps_previous_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discord-settings.bin"
            path.write_bytes(b"old-ciphertext")
            store = DiscordSettingsStore(path, protect=lambda _plain: b"new-ciphertext")
            with patch("discord_alerts.os.replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    store.save("https://discord.com/api/webhooks/1/token", "2")
            self.assertEqual(path.read_bytes(), b"old-ciphertext")
            self.assertFalse(path.with_suffix(".bin.tmp").exists())

    def test_corrupt_state_disables_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discord-settings.bin"
            path.write_bytes(b"ciphertext")
            store = DiscordSettingsStore(path, unprotect=lambda _data: b"not-json")
            self.assertIsNone(store.load())


class RepairRegressionTest(unittest.TestCase):
    def test_malformed_protected_payload_disables_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.bin"
            path.write_bytes(b"ciphertext")
            for payload in (b"null", b"[]", b'"text"', b'{"webhook_url":4,"user_id":"2"}',
                            b"[" * 2000 + b"0" + b"]" * 2000):
                with self.subTest(payload=payload):
                    store = DiscordSettingsStore(path, unprotect=lambda _data: payload)
                    self.assertIsNone(store.load())

    def test_own_process_window_is_not_bound(self):
        import os
        from discord_alerts import bind_foreground_target
        with patch("discord_alerts.os.name", "nt"), \
             patch("discord_alerts.ctypes.windll.user32.GetForegroundWindow", return_value=101), \
             patch("discord_alerts._window_target", return_value=WindowTarget(101, os.getpid(), 303)):
            self.assertIsNone(bind_foreground_target((0, 0, 10, 10)))

    def test_close_waits_for_capture_cleanup_and_never_delivers(self):
        entered, cleaned = threading.Event(), threading.Event()
        def capture(_target, cancelled):
            entered.set()
            cancelled.wait(2)
            cleaned.set()
        deliver = Mock()
        settings = DiscordSettings("https://discord.com/api/webhooks/1/token", "2", True)
        notifier = DiscordNotifier(MemoryStore(settings), capture=capture, deliver=deliver)
        self.assertTrue(notifier.schedule("+1"))
        self.assertTrue(entered.wait(1))
        notifier.close()
        self.assertTrue(cleaned.is_set())
        deliver.assert_not_called()

    def test_invalid_direct_delivery_never_opens_network(self):
        with patch("discord_alerts.build_opener") as opener:
            result = send_webhook(DiscordSettings("https://evil.test/x", "2"))
        opener.assert_not_called()
        self.assertEqual(result, "Discord alert failed.")


class CaptureBoundaryTest(unittest.TestCase):
    def test_helper_uses_only_the_explicit_hwnd(self):
        created = []

        class Capture:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                created.append(self)

            def event(self, handler):
                setattr(self, handler.__name__, handler)
                return handler

            def start(self):
                self.on_closed()

        connection = Mock()
        watch = Mock(identity=WindowTarget(101, 202, 303))
        with patch("discord_alerts.target_is_current", return_value=True), \
             patch("discord_alerts.WindowWatch", return_value=watch), \
             patch.dict(sys.modules, {"windows_capture": SimpleNamespace(WindowsCapture=Capture)}):
            capture_entry(connection, WindowTarget(101, 202, 303))
        self.assertEqual(created[0].kwargs,
                         {"cursor_capture": False, "secondary_window": False, "window_hwnd": 101})
        connection.send_bytes.assert_called_once_with(b"")


class WindowsLifecycleTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows capture identity")
    def test_destroyed_hwnd_cannot_become_valid_again(self):
        import tkinter as tk
        from discord_alerts import WindowWatch, _windows_api
        root = tk.Tk()
        root.update()
        user32, _kernel32 = _windows_api()
        watch = WindowWatch(user32.GetAncestor(root.winfo_id(), 2))
        try:
            self.assertTrue(watch.current())
            identity = watch.identity
            with patch("discord_alerts._window_target", return_value=identity):
                root.destroy()
                self.assertFalse(watch.current())
                self.assertFalse(watch.current())
        finally:
            watch.close()
            try:
                root.destroy()
            except tk.TclError:
                pass

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_dpapi_settings_round_trip_and_enabled_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.bin"
            store = DiscordSettingsStore(path)
            saved = store.save("https://discord.com/api/webhooks/1/synthetic", "2", True)
            self.assertNotIn(b"synthetic", path.read_bytes())
            self.assertEqual(store.load(), saved)
            store.clear()
            self.assertIsNone(store.load())


class DeliveryTest(unittest.TestCase):
    def settings(self):
        return DiscordSettings("https://discord.com/api/webhooks/123/token", "456", True)

    def test_delivery_mentions_only_configured_user_and_attaches_one_image(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch("discord_alerts.build_opener", return_value=opener):
            result = send_webhook(self.settings(), "+99", b"jpeg")
        request = opener.open.call_args.args[0]
        self.assertEqual(result, "Discord alert accepted.")
        self.assertTrue(request.full_url.endswith("?wait=true"))
        self.assertIn(b'"parse":[]', request.data)
        self.assertIn(b'"users":["456"]', request.data)
        self.assertIn(b'filename="game.jpg"', request.data)
        self.assertNotIn(b"@everyone", request.data)

    def test_timeout_is_ambiguous_and_does_not_retry(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError()
        with patch("discord_alerts.build_opener", return_value=opener):
            result = send_webhook(self.settings(), "+99")
        self.assertEqual(result, "Discord alert status unknown after timeout.")
        opener.open.assert_called_once()


    def test_http_errors_and_redirects_are_sanitized_without_retry(self):
        from urllib.error import HTTPError, URLError
        for error in (HTTPError("redacted", 302, "redirect", {}, None),
                      HTTPError("redacted", 429, "rate", {}, None),
                      HTTPError("redacted", 500, "server", {}, None),
                      URLError(TimeoutError())):
            opener = Mock()
            opener.open.side_effect = error
            with patch("discord_alerts.build_opener", return_value=opener):
                result = send_webhook(self.settings(), "+99")
            opener.open.assert_called_once()
            self.assertNotIn("redacted", result)
            self.assertNotIn("accepted", result)
            if isinstance(error, URLError) and isinstance(error.reason, TimeoutError):
                self.assertIn("unknown after timeout", result)

    def test_no_image_notice_and_explicit_test_label(self):
        from discord_alerts import _multipart, _NoRedirect
        normal, _ = _multipart(self.settings(), "+99", None, False)
        test, _ = _multipart(self.settings(), "", None, True)
        self.assertIn(b"Game image unavailable", normal)
        self.assertIn(b"Discord test alert", test)
        self.assertNotIn(b"files[0]", test)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))


class FrameEncodingTest(unittest.TestCase):
    def test_bgra_colors_size_limit_and_blank_fallback(self):
        from io import BytesIO
        import numpy as np
        from PIL import Image
        from discord_alerts import _jpeg_from_frame
        pixels = np.zeros((1000, 2000, 4), dtype=np.uint8)
        self.assertIsNone(_jpeg_from_frame(SimpleNamespace(frame_buffer=pixels)))
        pixels[:, :, 2] = 255
        encoded = _jpeg_from_frame(SimpleNamespace(frame_buffer=pixels))
        image = Image.open(BytesIO(encoded))
        self.assertEqual(image.size, (1920, 960))
        red, green, blue = image.getpixel((100, 100))
        self.assertGreater(red, 240)
        self.assertLess(blue, 10)
        self.assertLessEqual(len(encoded), 8 * 1024 * 1024)


class NotificationCancellationTest(unittest.TestCase):
    def setUp(self):
        self.settings = DiscordSettings("https://discord.com/api/webhooks/123/token", "456", True)

    def test_disabled_and_busy_hits_do_not_start_jobs(self):
        release = threading.Event()
        notifier = DiscordNotifier(MemoryStore(self.settings),
                                   capture=lambda *_args, **_kwargs: release.wait(1), deliver=Mock())
        self.assertTrue(notifier.schedule("+1", WindowTarget(1, 2, 3)))
        self.assertFalse(notifier.schedule("+2", WindowTarget(1, 2, 3)))
        release.set()
        notifier = DiscordNotifier(MemoryStore(DiscordSettings(self.settings.webhook_url, self.settings.user_id, False)),
                                   capture=Mock(), deliver=Mock())
        self.assertFalse(notifier.schedule("+1"))

    def test_shutdown_wins_submission_gate_without_a_request(self):
        deliver = Mock()
        notifier = DiscordNotifier(MemoryStore(self.settings), capture=lambda *_args, **_kwargs: None, deliver=deliver)
        notifier.close()
        notifier._run(_Job(self.settings, 0, "+1", None, False))
        deliver.assert_not_called()

    def test_disable_or_settings_change_cancels_pending_request(self):
        for operation in (lambda n: n.set_enabled(False),
                          lambda n: n.save_settings("https://discord.com/api/webhooks/3/new", "4"),
                          lambda n: n.clear()):
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            def capture(_target, cancelled):
                entered.set()
                release.wait(2)
            deliver = Mock()
            notifier = DiscordNotifier(MemoryStore(self.settings), capture=capture, deliver=deliver,
                                       log=lambda message: finished.set() if "skipped" in message else None)
            self.assertTrue(notifier.schedule("+1"))
            self.assertTrue(entered.wait(1))
            operation(notifier)
            release.set()
            self.assertTrue(finished.wait(1))
            notifier.close()
            deliver.assert_not_called()

    def test_submitted_request_is_not_recalled_by_disable_or_close(self):
        submitted, release, finished = threading.Event(), threading.Event(), threading.Event()
        def deliver(*_args):
            submitted.set()
            release.wait(2)
            finished.set()
            return "Discord alert accepted."
        notifier = DiscordNotifier(MemoryStore(self.settings), capture=lambda *_a, **_k: None,
                                   deliver=deliver)
        self.assertTrue(notifier.schedule("+1"))
        self.assertTrue(submitted.wait(1))
        notifier.set_enabled(False)
        notifier.close()
        self.assertFalse(notifier.schedule("+2"))
        release.set()
        self.assertTrue(finished.wait(1))

    def test_test_alert_is_text_only_and_allowed_while_disabled(self):
        settings = DiscordSettings(self.settings.webhook_url, self.settings.user_id, False)
        deliver = Mock(return_value="Discord alert accepted.")
        notifier = DiscordNotifier(MemoryStore(settings), capture=Mock(), deliver=deliver)
        notifier._run(_Job(settings, 0, "", None, True))
        deliver.assert_called_once_with(settings, "", None, True)


if __name__ == "__main__":
    unittest.main()
