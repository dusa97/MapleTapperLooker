"""Behavior checks for Discord settings, delivery, and capture boundaries."""
import json
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
        with patch("discord_alerts.target_is_current", return_value=True), \
             patch("windows_capture.WindowsCapture", Capture):
            capture_entry(connection, WindowTarget(101, 202, 303))
        self.assertEqual(created[0].kwargs,
                         {"cursor_capture": False, "secondary_window": False, "window_hwnd": 101})
        connection.send.assert_called_once_with(None)


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

    def test_test_alert_is_text_only_and_allowed_while_disabled(self):
        settings = DiscordSettings(self.settings.webhook_url, self.settings.user_id, False)
        deliver = Mock(return_value="Discord alert accepted.")
        notifier = DiscordNotifier(MemoryStore(settings), capture=Mock(), deliver=deliver)
        notifier._run(_Job(settings, 0, "", None, True))
        deliver.assert_called_once_with(settings, "", None, True)


if __name__ == "__main__":
    unittest.main()
