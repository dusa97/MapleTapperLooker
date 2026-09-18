"""Private Discord hit alerts with Windows-only protected settings and capture."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from io import BytesIO
import ctypes
from ctypes import wintypes
from functools import lru_cache
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
CAPTURE_FAILURES = {
    b"unbound": "no target was bound. Focus the game before F9.",
    b"own-window": "the selected window belongs to this app. Focus the game before F9.",
    b"region-outside": "the watched region is outside the selected window.",
    b"identity": "the target is closed, hidden, minimized, or its identity cannot be verified.",
    b"blank": "Windows returned a blank or unsupported frame.",
    b"closed": "Windows closed capture before an image arrived.",
    b"timeout": "capture exceeded the five-second limit.",
    b"invalid-image": "the returned image failed validation.",
    b"helper-failed": "the Windows capture helper failed.",
}


def _capture_failure(report, reason):
    if report is not None:
        report("Discord image unavailable: " + CAPTURE_FAILURES.get(reason, CAPTURE_FAILURES[b"helper-failed"]))
    return None


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
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value) or not (0 < int(value) <= MAX_SNOWFLAKE):
        raise ValueError(f"Enter a valid {field}.")
    return value


def validate_settings(webhook_url: str, user_id: str) -> DiscordSettings:
    """Validate local fields before storage or network I/O."""
    if not isinstance(webhook_url, str) or not webhook_url.startswith("https://discord.com/"):
        raise ValueError("Enter a canonical Discord webhook URL.")
    if any(ord(c) <= 32 or ord(c) >= 127 for c in webhook_url) or "?" in webhook_url or "#" in webhook_url:
        raise ValueError("Enter a canonical Discord webhook URL.")
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
            if not isinstance(payload, dict) or type(payload.get("enabled", False)) is not bool:
                return None
            settings = validate_settings(payload["webhook_url"], payload["user_id"])
            return replace(settings, enabled=payload.get("enabled", False))
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
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


@lru_cache(maxsize=1)
def _windows_api():
    """Declare pointer-sized Windows handles before use."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    for name in ("GetForegroundWindow", "GetDesktopWindow", "GetShellWindow", "GetAncestor"):
        getattr(user32, name).restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
        getattr(user32, name).argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    return user32, kernel32


def _process_creation_time(pid: int) -> int | None:
    if os.name != "nt" or not pid:
        return None
    _windows_api()
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


def _window_target(hwnd: int, region: tuple | list | None = None, report=None) -> WindowTarget | None:
    if os.name != "nt" or not hwnd:
        return None
    user32, _kernel32 = _windows_api()
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
            return _capture_failure(report, b"region-outside")
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    created = _process_creation_time(pid.value)
    return WindowTarget(hwnd, pid.value, created) if created is not None else None


def bind_foreground_target(region: tuple | list, own_hwnds=(), report=None) -> WindowTarget | None:
    if os.name != "nt":
        return None
    user32, _kernel32 = _windows_api()
    hwnd = user32.GetForegroundWindow()
    if hwnd in set(own_hwnds):
        return _capture_failure(report, b"own-window")
    identity = _window_target(hwnd, region, report=report)
    if identity is None:
        return None
    if identity.pid == os.getpid():
        return _capture_failure(report, b"own-window")
    watch = WindowWatch(hwnd)
    if watch.identity != identity or not watch.current():
        watch.close()
        return None
    return watch


def target_is_current(target: WindowTarget | None) -> bool:
    if isinstance(target, WindowWatch):
        return target.current()
    if not isinstance(target, WindowTarget):
        return False
    for value, bits in ((target.hwnd, ctypes.sizeof(ctypes.c_void_p) * 8),
                        (target.pid, 32), (target.creation_time, 64)):
        if type(value) is not int or not (0 < value < 1 << bits):
            return False
    current = _window_target(target.hwnd)
    return current is not None and current.pid == target.pid and current.creation_time == target.creation_time


class WindowWatch:
    """Keep destruction irreversible while an activation or helper uses a HWND."""
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.identity = None
        self._invalid = threading.Event()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._checks = queue.Queue()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        if not self._ready.wait(1):
            self._invalid.set()

    def _watch(self):
        hook = None
        try:
            user32, _kernel32 = _windows_api()
            callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                               wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
            def destroyed(_hook, _event, hwnd, object_id, child_id, _thread, _time):
                if hwnd == self.hwnd and object_id == 0 and child_id == 0:
                    self._invalid.set()
            callback = callback_type(destroyed)
            user32.SetWinEventHook.restype = wintypes.HANDLE
            user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                               callback_type, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
            user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
            hook = user32.SetWinEventHook(0x8001, 0x8001, None, callback, 0, 0, 0)
            if not hook:
                self._invalid.set()
                return
            self.identity = _window_target(self.hwnd)
            self._ready.set()
            message = wintypes.MSG()
            while not self._stop.is_set():
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                if self.identity is None or _window_target(self.hwnd) != self.identity:
                    self._invalid.set()
                while True:
                    try:
                        self._checks.get_nowait().set()
                    except queue.Empty:
                        break
                self._stop.wait(0.01)
        except Exception:
            self._invalid.set()
        finally:
            self._invalid.set()
            self._ready.set()
            if hook:
                user32.UnhookWinEvent(hook)

    def current(self):
        if self._invalid.is_set() or not self._thread.is_alive():
            return False
        checked = threading.Event()
        self._checks.put(checked)
        return checked.wait(0.25) and not self._invalid.is_set()

    def close(self):
        self._invalid.set()
        self._stop.set()
        self._thread.join()


def _jpeg_from_frame(frame) -> bytes | None:
    from PIL import Image
    buffer = frame.frame_buffer
    if len(buffer.shape) != 3 or buffer.shape[2] != 4 or buffer.shape[0] * buffer.shape[1] > MAX_CAPTURE_PIXELS:
        return None
    image = Image.fromarray(buffer[:, :, [2, 1, 0, 3]].copy(), "RGBA").convert("RGB")
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
    watch = None
    try:
        if not target_is_current(target):
            connection.send_bytes(b"identity")
            return
        watch = WindowWatch(target.hwnd)
        from windows_capture import WindowsCapture
        if watch.identity != target or not watch.current() or not target_is_current(target):
            connection.send_bytes(b"identity")
            return
        capture = WindowsCapture(cursor_capture=False, secondary_window=False, window_hwnd=target.hwnd)
        sent = False

        @capture.event
        def on_frame_arrived(frame, control):
            nonlocal sent
            result = _jpeg_from_frame(frame) if watch.current() and target_is_current(target) else None
            reason = b"blank"
            if not watch.current() or not target_is_current(target):
                result = None
                reason = b"identity"
            if not sent:
                connection.send_bytes(result or reason)
                sent = True
            control.stop()

        @capture.event
        def on_closed():
            if not sent:
                connection.send_bytes(b"closed")

        capture.start()
    except Exception:
        try:
            connection.send_bytes(b"helper-failed")
        except (BrokenPipeError, OSError):
            pass
    finally:
        if watch is not None:
            watch.close()
        connection.close()


def capture_window(target: WindowTarget | None, timeout: float = 5.0,
                   cancelled: threading.Event | None = None, context=None, report=None) -> bytes | None:
    """Capture only a verified HWND in a bounded spawned helper."""
    deadline = time.monotonic() + min(timeout, 5.0)
    if cancelled is not None and cancelled.is_set():
        return None
    if target is None:
        return _capture_failure(report, b"unbound")
    if not target_is_current(target):
        return _capture_failure(report, b"identity")
    identity = target.identity if isinstance(target, WindowWatch) else target
    context = context or multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=capture_entry, args=(child, identity))
    started = False
    try:
        with getattr(cancelled, "start_lock", nullcontext()):
            if cancelled is not None and cancelled.is_set():
                return None
            process.start()
            started = True
        child.close()
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                return None
            if parent.poll(0.05):
                result = parent.recv_bytes(MAX_IMAGE_BYTES)
                if time.monotonic() >= deadline:
                    return _capture_failure(report, b"timeout")
                if not target_is_current(target):
                    return _capture_failure(report, b"identity")
                if result in CAPTURE_FAILURES:
                    return _capture_failure(report, result)
                if not isinstance(result, bytes) or len(result) > MAX_IMAGE_BYTES:
                    return _capture_failure(report, b"invalid-image")
                from PIL import Image
                try:
                    with Image.open(BytesIO(result)) as image:
                        if image.format != "JPEG" or not (0 < image.width <= MAX_IMAGE_EDGE and 0 < image.height <= MAX_IMAGE_EDGE):
                            return _capture_failure(report, b"invalid-image")
                        image.verify()
                except Exception:
                    return _capture_failure(report, b"invalid-image")
                return result
        return _capture_failure(report, b"timeout")
    except (EOFError, OSError):
        return _capture_failure(report, b"helper-failed")
    finally:
        if started:
            if process.is_alive():
                process.terminate()
            process.join()
            process.close()
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
    try:
        validate_settings(settings.webhook_url, settings.user_id)
    except (ValueError, TypeError):
        return "Discord alert failed."
    body, boundary = _multipart(settings, value, image, test)
    request = Request(settings.webhook_url + "?wait=true", body,
                      {"Content-Type": f"multipart/form-data; boundary={boundary}",
                       "User-Agent": "MapleTapperLooker (https://github.com/dusa97/MapleTapperLooker, 1.0)"},
                      method="POST")
    try:
        opener = build_opener(_NoRedirect)
        with opener.open(request, timeout=10):
            return "Discord alert accepted."
    except socket.timeout:
        return "Discord alert status unknown after timeout."
    except HTTPError as error:
        return "Discord alert failed." if error.code != 429 else "Discord alert failed (rate limited)."
    except URLError as error:
        return ("Discord alert status unknown after timeout." if isinstance(error.reason, TimeoutError)
                else "Discord alert failed.")
    except (OSError, ssl.SSLError):
        return "Discord alert failed."


@dataclass(frozen=True)
class _Job:
    settings: DiscordSettings
    revision: int
    value: str
    target: WindowTarget | None
    test: bool
    activation: int | None = None


class DiscordNotifier:
    """One cancellable alert at a time. UI callbacks only enqueue activity text."""
    def __init__(self, store: DiscordSettingsStore | None = None, log: Callable[[str], None] | None = None,
                 capture: Callable | None = None, deliver: Callable = send_webhook):
        self.store = store or DiscordSettingsStore()
        self.log = log or (lambda _message: None)
        self.capture = capture or (lambda target, cancelled: capture_window(target, cancelled=cancelled, report=self.log))
        self.deliver = deliver
        self._lock = threading.Lock()
        self._settings = self.store.load()
        self._enabled = bool(self._settings and self._settings.enabled)
        self._revision = 0
        self._busy = False
        self._submitted = False
        self._closing = False
        self._cancel = threading.Event()
        self._worker = None
        self._capture_done = threading.Event()
        self._capture_done.set()

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
            self._settings, self._revision = saved, self._revision + 1
            self._cancel.set()

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            if enabled and self._settings is None:
                return False
            settings = self._settings
            if settings is not None:
                saved = self.store.save(settings.webhook_url, settings.user_id, enabled)
                self._settings, self._enabled, self._revision = saved, enabled, self._revision + 1
            else:
                self._revision += 1
            self._cancel.set()
            return not enabled or settings is not None

    def clear(self) -> None:
        with self._lock:
            self.store.clear()
            self._settings = None
            self._enabled = False
            self._revision += 1
            self._cancel.set()

    def schedule(self, value: str, target: WindowTarget | None = None, test: bool = False,
                 *, activation: int | None = None) -> bool:
        with self._lock:
            if self._closing or self._busy or self._settings is None or (not test and not self._enabled):
                return False
            self._busy = True
            self._submitted = False
            job = _Job(self._settings, self._revision, value, target, test, activation)
            self._cancel = threading.Event()
            # Use the state lock to order helper startup against cancellation.
            self._cancel.start_lock = self._lock
            self._capture_done.clear()
            self._worker = threading.Thread(target=self._run, args=(job,), daemon=True)
            self.log("Discord alert pending.")
            try:
                self._worker.start()
            except RuntimeError:
                self._busy = False
                self._capture_done.set()
                self.log("Discord alert failed.")
                return False
            return True

    def _run(self, job: _Job) -> None:
        try:
            try:
                image = None if job.test else self.capture(job.target, cancelled=self._cancel)
            except Exception:
                image = _capture_failure(self.log, b"helper-failed")
            finally:
                self._capture_done.set()
            if image is not None and not target_is_current(job.target):
                image = _capture_failure(self.log, b"identity")
            with self._lock:
                allowed = not self._closing and job.revision == self._revision and (job.test or self._enabled)
                self._submitted = allowed
            if not allowed:
                self.log("Discord alert skipped.")
            else:
                self.log(self.deliver(job.settings, job.value, image, job.test))
        except Exception:
            self.log("Discord alert failed.")
        finally:
            with self._lock:
                self._submitted = False
                self._busy = False

    def close(self) -> None:
        with self._lock:
            self._closing = True
            self._revision += 1
            self._cancel.set()
        self._capture_done.wait()
