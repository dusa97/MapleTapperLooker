"""Private Discord hit alerts with Windows-only protected settings and capture."""
from __future__ import annotations

from dataclasses import dataclass, replace
from io import BytesIO
import ctypes
import json
import multiprocessing
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_SNOWFLAKE = (1 << 64) - 1
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_EDGE = 1920
MAX_CAPTURE_PIXELS = 64_000_000
WEBHOOK_RE = re.compile(r"^/api(?:/v10)?/webhooks/([1-9][0-9]*)/([A-Za-z0-9._-]+)$")


@dataclass(frozen=True)
class DiscordSettings:
    webhook_url: str
    user_id: str
    enabled: bool = False


@dataclass(frozen=True)
class WindowTarget:
    hwnd: int
    pid: int
    creation_time: int


def _snowflake(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.isdecimal() or not (0 < int(value) <= MAX_SNOWFLAKE):
        raise ValueError(f"Enter a valid {field}.")
    return value


def validate_settings(webhook_url: str, user_id: str) -> DiscordSettings:
    """Validate local fields before storage or network I/O."""
    parsed = urlsplit(webhook_url)
    if (parsed.scheme != "https" or parsed.hostname != "discord.com" or parsed.netloc != "discord.com"
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("Enter a Discord webhook URL for discord.com.")
    match = WEBHOOK_RE.fullmatch(parsed.path)
    if not match:
        raise ValueError("Enter a canonical Discord webhook URL.")
    _snowflake(match.group(1), "webhook ID")
    return DiscordSettings(webhook_url, _snowflake(user_id, "Discord user ID"))


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("Discord settings need Windows DPAPI.")
    source = ctypes.create_string_buffer(data)
    source_blob = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source_blob), "MapleTapperLooker", None,
                                                   None, None, 0, ctypes.byref(output)):
        raise OSError("Could not protect Discord settings.")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("Discord settings need Windows DPAPI.")
    source = ctypes.create_string_buffer(data)
    source_blob = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source_blob), None, None, None, None,
                                                     0, ctypes.byref(output)):
        raise OSError("Could not open Discord settings.")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


class DiscordSettingsStore:
    """DPAPI storage that never writes a plaintext settings file."""
    def __init__(self, path: Path | str | None = None, protect=_protect, unprotect=_unprotect):
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "MapleTapperLooker"
        self.path = Path(path) if path is not None else base / "discord-settings.bin"
        self.protect = protect
        self.unprotect = unprotect

    @property
    def supported(self) -> bool:
        return os.name == "nt"

    def load(self) -> DiscordSettings | None:
        try:
            payload = json.loads(self.unprotect(self.path.read_bytes()).decode("utf-8"))
            settings = validate_settings(payload["webhook_url"], payload["user_id"])
            return replace(settings, enabled=bool(payload.get("enabled", False)))
        except (OSError, ValueError, KeyError, UnicodeError, json.JSONDecodeError):
            return None

    def save(self, webhook_url: str, user_id: str, enabled: bool = False) -> DiscordSettings:
        settings = replace(validate_settings(webhook_url, user_id), enabled=bool(enabled))
        plaintext = json.dumps({"webhook_url": settings.webhook_url, "user_id": settings.user_id,
                                "enabled": settings.enabled}, separators=(",", ":")).encode("utf-8")
        ciphertext = self.protect(plaintext)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, "xb") as output:
                output.write(ciphertext)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise OSError("Could not save Discord settings.") from None
        return settings

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise OSError("Could not clear Discord settings.") from None


def _process_creation_time(pid: int) -> int | None:
    if os.name != "nt" or not pid:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = ctypes.c_ulonglong()
        ignored = ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ignored),
                                                       ctypes.byref(ignored), ctypes.byref(ignored)):
            return None
        return created.value
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _window_target(hwnd: int, region: tuple | list | None = None) -> WindowTarget | None:
    if os.name != "nt" or not hwnd:
        return None
    user32 = ctypes.windll.user32
    if (not user32.IsWindow(hwnd) or user32.GetAncestor(hwnd, 2) != hwnd
            or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd)
            or hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow())):
        return None
    rect = (ctypes.c_long * 4)()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    if region is not None:
        left, top, width, height = region
        if left < rect[0] or top < rect[1] or left + width > rect[2] or top + height > rect[3]:
            return None
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    created = _process_creation_time(pid.value)
    return WindowTarget(hwnd, pid.value, created) if created is not None else None


def bind_foreground_target(region: tuple | list, own_hwnds=()) -> WindowTarget | None:
    if os.name != "nt":
        return None
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if hwnd in set(own_hwnds):
        return None
    return _window_target(hwnd, region)


def target_is_current(target: WindowTarget | None) -> bool:
    if target is None:
        return False
    current = _window_target(target.hwnd)
    return current is not None and current.pid == target.pid and current.creation_time == target.creation_time


def _jpeg_from_frame(frame) -> bytes | None:
    from PIL import Image
    image = Image.fromarray(frame.frame_buffer.copy(), "RGBA").convert("RGB")
    if not image.width or not image.height or image.width * image.height > MAX_CAPTURE_PIXELS:
        return None
    if all(high == 0 for _low, high in image.getextrema()):
        return None
    if max(image.size) > MAX_IMAGE_EDGE:
        scale = MAX_IMAGE_EDGE / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True)
    result = output.getvalue()
    return result if len(result) <= MAX_IMAGE_BYTES else None


def capture_entry(connection, target: WindowTarget) -> None:
    """Spawn-safe helper entry. It receives identity only, never credentials."""
    try:
        if not target_is_current(target):
            connection.send(None)
            return
        from windows_capture import WindowsCapture
        capture = WindowsCapture(cursor_capture=False, secondary_window=False, window_hwnd=target.hwnd)
        sent = False

        @capture.event
        def on_frame_arrived(frame, control):
            nonlocal sent
            result = _jpeg_from_frame(frame) if target_is_current(target) else None
            if not target_is_current(target):
                result = None
            if not sent:
                connection.send(result)
                sent = True
            control.stop()

        @capture.event
        def on_closed():
            if not sent:
                connection.send(None)

        capture.start()
    except Exception:
        try:
            connection.send(None)
        except (BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


def capture_window(target: WindowTarget | None, timeout: float = 5.0,
                   cancelled: threading.Event | None = None, context=None) -> bytes | None:
    """Capture only a verified HWND in a bounded spawned helper."""
    if not target_is_current(target):
        return None
    context = context or multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=capture_entry, args=(child, target))
    try:
        process.start()
        child.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                return None
            if parent.poll(0.05):
                result = parent.recv()
                if not target_is_current(target):
                    return None
                return result if isinstance(result, bytes) and len(result) <= MAX_IMAGE_BYTES else None
        return None
    except (EOFError, OSError):
        return None
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        parent.close()
        try:
            child.close()
        except OSError:
            pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _multipart(settings: DiscordSettings, value: str, image: bytes | None, test: bool) -> tuple[bytes, str]:
    boundary = "MapleTapperLookerDiscord"
    text = f"<@{settings.user_id}> " + ("Discord test alert." if test else f"Confirmed hit: {value}.")
    if image is None and not test:
        text += " Game image unavailable."
    payload = json.dumps({"content": text, "allowed_mentions": {"parse": [], "users": [settings.user_id],
                         "roles": [], "replied_user": False}}, separators=(",", ":")).encode()
    lines = [b"--" + boundary.encode(), b'Content-Disposition: form-data; name="payload_json"',
             b"Content-Type: application/json", b"", payload]
    if image is not None:
        lines += [b"--" + boundary.encode(),
                  b'Content-Disposition: form-data; name="files[0]"; filename="game.jpg"',
                  b"Content-Type: image/jpeg", b"", image]
    lines += [b"--" + boundary.encode() + b"--", b""]
    return b"\r\n".join(lines), boundary


def send_webhook(settings: DiscordSettings, value: str = "", image: bytes | None = None,
                 test: bool = False) -> str:
    """Send one request. Returned states are safe for the activity log."""
    body, boundary = _multipart(settings, value, image, test)
    request = Request(settings.webhook_url + "?wait=true", body,
                      {"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    try:
        opener = build_opener(_NoRedirect)
        with opener.open(request, timeout=10):
            return "Discord alert accepted."
    except socket.timeout:
        return "Discord alert status unknown after timeout."
    except HTTPError as error:
        return "Discord alert failed." if error.code != 429 else "Discord alert failed (rate limited)."
    except (URLError, OSError, ssl.SSLError):
        return "Discord alert failed."


@dataclass(frozen=True)
class _Job:
    settings: DiscordSettings
    revision: int
    value: str
    target: WindowTarget | None
    test: bool


class DiscordNotifier:
    """One cancellable alert at a time. UI callbacks only enqueue activity text."""
    def __init__(self, store: DiscordSettingsStore | None = None, log: Callable[[str], None] | None = None,
                 capture: Callable = capture_window, deliver: Callable = send_webhook):
        self.store = store or DiscordSettingsStore()
        self.log = log or (lambda _message: None)
        self.capture = capture
        self.deliver = deliver
        self._lock = threading.Lock()
        self._settings = self.store.load()
        self._enabled = bool(self._settings and self._settings.enabled)
        self._revision = 0
        self._busy = False
        self._submitted = False
        self._closing = False
        self._cancel = threading.Event()

    @property
    def supported(self) -> bool:
        return self.store.supported

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def settings(self) -> DiscordSettings | None:
        with self._lock:
            return self._settings

    def save_settings(self, webhook_url: str, user_id: str) -> None:
        with self._lock:
            enabled = self._enabled
        saved = self.store.save(webhook_url, user_id, enabled)
        with self._lock:
            self._settings, self._revision = saved, self._revision + 1

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            if enabled and self._settings is None:
                return False
            settings = self._settings
        if settings is not None:
            saved = self.store.save(settings.webhook_url, settings.user_id, enabled)
            with self._lock:
                self._settings, self._enabled, self._revision = saved, enabled, self._revision + 1
        return not enabled or settings is not None

    def clear(self) -> None:
        self.store.clear()
        with self._lock:
            self._settings = None
            self._enabled = False
            self._revision += 1

    def schedule(self, value: str, target: WindowTarget | None = None, test: bool = False) -> bool:
        with self._lock:
            if self._closing or self._busy or self._settings is None or (not test and not self._enabled):
                return False
            self._busy = True
            self._submitted = False
            job = _Job(self._settings, self._revision, value, target, test)
            self._cancel = threading.Event()
        self.log("Discord alert pending.")
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return True

    def _run(self, job: _Job) -> None:
        image = None if job.test else self.capture(job.target, cancelled=self._cancel)
        with self._lock:
            allowed = not self._closing and job.revision == self._revision and (job.test or self._enabled)
            # This is the atomic pending-to-submitted transition. A later close
            # cannot recall the request, so it must not block this winner.
            self._submitted = allowed
        if not allowed:
            self.log("Discord alert skipped.")
        else:
            self.log(self.deliver(job.settings, job.value, image, job.test))
        with self._lock:
            self._submitted = False
            self._busy = False

    def close(self) -> None:
        with self._lock:
            self._closing = True
            self._revision += 1
            self._cancel.set()
