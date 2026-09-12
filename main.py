#!/usr/bin/env python3
"""
Draggable/resizable green box overlay that OCRs the "+<number>" delta inside
it — e.g. "+538,683" from a "Combat Power Change" popup (see +delata_ocr.png:
bold white text with a black outline on a dark green/olive panel).

Position the box over the number, and it's continuously screenshotted,
preprocessed (upscaled + grayscaled), and OCR'd with Tesseract. Whenever a
"+<digits>" match appears, a separate draggable status panel (off to the
side, so it never covers the box or gets OCR'd itself) shows it and it's
printed to the console; a beep fires once per new value (not every frame
the popup happens to still be on screen). The OCR loop runs on its own
background thread, back-to-back
with no artificial delay between reads, so detection latency is bounded only
by how fast screen-capture + Tesseract actually are (~150ms/read) instead of
also waiting out a fixed poll interval on top of that.

Press F9 (global — works even while the game has focus) to start spamming
Enter + left-click. It keeps spamming until the box detects a "+<number>",
at which point it beeps once and stops spamming on its own — press F9 again
to resume.

Press F11 to record a microphone message. Press F11 again to save it.
The saved message replaces the detection beep. Repeat to replace the message.
F12 deletes the saved message and restores the beep.
F10 mutes both the message and the beep. Recording requires Windows.

Requirements:
    pip install pillow pytesseract mss keyboard pydirectinput
Tesseract OCR engine:
    - Windows : https://github.com/UB-Mannheim/tesseract/wiki
    - macOS   : brew install tesseract
    - Linux   : sudo apt install tesseract-ocr
"""

import re
import sys
import time
import json
import random
import queue
import platform
import threading
import tkinter as tk
from tkinter import font as tkfont
from pathlib import Path

from recorded_alert import RecordedAlert

try:
    import mss              # screen capture — ~2x faster and far more consistent than pyautogui.screenshot
    import pytesseract
    from PIL import Image
    import keyboard        # global hotkey to start/stop Enter+click spam — works even while the game has focus
    import pydirectinput   # DirectInput-style key injection — see the spam-loop comment below
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install pillow pytesseract mss keyboard pydirectinput")
    sys.exit(1)

pydirectinput.FAILSAFE = False   # don't abort if the mouse happens to be at a screen corner
pydirectinput.PAUSE    = 0       # no built-in delay after every press()/click() call —
                                  # our own enter_interval/click_interval control the cadence instead

# The `tesseract` binary (not just the pytesseract wrapper) is required — if it's
# not yet on PATH (e.g. right after installing it, before a new shell picks up
# the PATH change), fall back to the standard Windows install location.
if platform.system() == "Windows":
    import shutil
    if not shutil.which("tesseract"):
        _default_tesseract = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if _default_tesseract.exists():
            pytesseract.pytesseract.tesseract_cmd = str(_default_tesseract)

# ── Elevation (Windows) ──────────────────────────────────────────────────────────
# keyboard.add_hotkey() installs a real global low-level hook — it does NOT need
# this window focused. The one thing that DOES break that: if the game itself
# runs elevated (as Administrator — common with anti-cheat) and this script does
# not, Windows' UIPI blocks a lower-privilege hook from seeing keystrokes while
# the elevated window has focus. Running this script elevated too fixes it.

def _is_admin() -> bool:
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_as_admin():
    """Re-launch this same script/exe elevated (triggers a UAC prompt) and exit."""
    import ctypes
    skip = {"--elevate", "--no-elevate"}
    params = " ".join(
        f'"{a}"' if " " in a else a for a in sys.argv[1:] if a not in skip
    )
    if getattr(sys, "frozen", False):
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    else:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{__file__}" {params}', None, 1)
    sys.exit(0)

# ── Config ───────────────────────────────────────────────────────────────────

# When frozen by PyInstaller, __file__ resolves inside the bundle's internal/
# temp extraction path (not next to the .exe), so last_region.json would never
# be found (or persisted) between runs — always forcing the region selector
# open. Anchor to the .exe's own directory in that case instead.
if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).parent
REGION_FILE     = _BASE_DIR / "last_region.json"
MIN_READ_GAP      = 0.0    # optional extra delay between OCR reads (0 = back-to-back, as fast as possible)
RENDER_INTERVAL   = 0.05   # seconds between UI repaints — independent of the OCR read cadence
OCR_TARGET_HEIGHT = 100    # preprocess_for_ocr upscales toward this fixed output height instead of a fixed
                           # multiplier — on a higher-resolution screen the same UI renders at more raw pixels,
                           # so a fixed multiplier (the old OCR_SCALE) blew the image up further and slowed
                           # every read down, widening the window where a spammed click could still land
                           # between the "+" appearing and this loop noticing it. Capping the target keeps
                           # per-read latency roughly constant across screen sizes/regions. Never downscales
                           # (min scale 1.0) or upscales past 4x (min-caps overhead on an already-huge region).
OCR_SCALE_MIN     = 1.0
OCR_SCALE_MAX     = 4.0
BEEP_COOLDOWN     = 1.5    # min seconds between beeps

ENTER_HOTKEY    = "f9"    # toggles Enter+Left-Click spam on/off — starts OFF
ENTER_INTERVAL  = 0.16    # seconds between spammed Enter presses (160ms)
CLICK_INTERVAL  = 0.24    # seconds between spammed left-clicks (240ms)
SPAM_JITTER     = 0.3     # +/- fraction of randomness applied to each interval above,
                          # so presses/clicks don't land on a perfectly robotic fixed cadence
BEEP_HOTKEY     = "f10"   # mutes/unmutes the detection beep — starts UNMUTED

# psm 6 = "uniform block of text" — the box may contain the label line above
# the number, so don't assume a single line. Whitelist keeps OCR focused on
# the characters a delta number can actually contain. Plain grayscale+upscale
# (no binarization) turned out to OCR this font far more reliably than any
# brightness threshold — thresholding was breaking up the dashed/outlined
# strokes of the game's stylized digits.
OCR_CONFIG = r'--psm 6 -c tessedit_char_whitelist=+-0123456789,'

PLUS_NUMBER_RE = re.compile(r'\+[\d,]{2,}')


def preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """Upscale + grayscale — small popup text needs the extra resolution.
    Scales toward OCR_TARGET_HEIGHT rather than a fixed multiplier, so a
    larger raw region (e.g. from a higher screen resolution) doesn't balloon
    processing time — see OCR_TARGET_HEIGHT for why that matters here."""
    scale = OCR_TARGET_HEIGHT / img.height
    scale = max(OCR_SCALE_MIN, min(scale, OCR_SCALE_MAX))
    big = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    return big.convert("L")


def _grab(region, sct=None) -> Image.Image:
    """Screenshot *region* = (left, top, w, h). Pass a reused mss instance via
    *sct* in a hot loop — creating one per call adds needless overhead."""
    left, top, w, h = (int(v) for v in region)
    box = {"left": left, "top": top, "width": w, "height": h}
    if sct is not None:
        raw = sct.grab(box)
    else:
        with mss.MSS() as s:
            raw = s.grab(box)
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def read_delta(region, sct=None) -> str | None:
    """Screenshot *region* and return the first "+<number>" match found, or None."""
    img = _grab(region, sct=sct)
    proc = preprocess_for_ocr(img)
    text = pytesseract.image_to_string(proc, config=OCR_CONFIG)
    m = PLUS_NUMBER_RE.search(text.replace(" ", ""))
    return m.group(0) if m else None


def beep():
    system = platform.system()
    if system == "Windows":
        import winsound; winsound.Beep(1000, 300)
    elif system == "Darwin":
        import os; os.system("afplay /System/Library/Sounds/Ping.aiff")
    else:
        import os
        if os.system("paplay /usr/share/sounds/freedesktop/stereo/bell.oga 2>/dev/null") != 0:
            print("\a", end="", flush=True)

# ── Region persistence ──────────────────────────────────────────────────────

def save_region(region: tuple):
    with open(REGION_FILE, "w") as f:
        json.dump({"region": list(region)}, f)


def load_region() -> tuple | None:
    if REGION_FILE.exists():
        try:
            data = json.loads(REGION_FILE.read_text())
            r = tuple(data["region"])
            print(f"[region] Loaded saved region {r}", flush=True)
            return r
        except Exception:
            pass
    return None

# ── Persistent overlay rectangle ────────────────────────────────────────────

class OverlayApp:
    """
    A green detection box with a click-through center and four resize handles.
    F8 opens a new selection. A background thread reads the selected region.
    """
    BORDER      = 3
    COLOR       = "#00FF88"
    COLOR_HIT   = "#FF4444"
    HANDLE_SIZE = 8
    MIN_SIZE    = 20

    def __init__(self, region: tuple, enter_spam: bool = True,
                 enter_hotkey: str = ENTER_HOTKEY,
                 enter_interval: float = ENTER_INTERVAL,
                 click_interval: float = CLICK_INTERVAL,
                 beep_hotkey: str = BEEP_HOTKEY,
                 min_read_gap: float = MIN_READ_GAP):
        self.region      = list(region)   # [x, y, w, h] — mutable, moved/resized live
        self.status_text = "paused"
        self.hit          = False
        self._prev_hit    = False   # edge-detect found/not-found so beep+stop fires once per detection
        self.last_value    = None
        self.last_beep_time = 0.0
        self.min_read_gap  = min_read_gap
        self.root         = None
        self._canvas      = None
        self._rect_id     = None
        self._handle_ids  = {}
        self._drag        = {}
        self._ocr_thread  = None
        self._panel        = None
        self._panel_status = None
        self._panel_enter  = None
        self._panel_beep   = None
        self._panel_drag   = None

        # Enter+Left-Click spam — starts OFF, F9 toggles it on; a detection
        # beeps once (unless muted) and turns it back off until F9 is pressed
        # again. OCR and detection audio run only while F9 is active.
        self.enter_spam_enabled = enter_spam
        self.enter_hotkey       = enter_hotkey
        self.enter_interval     = enter_interval
        self.click_interval     = click_interval
        self.enter_on           = False
        self._activation        = 0
        self._lock_rect         = None   # active ClipCursor rect while enter_on is True, re-applied by _spam_loop
        self._running           = True
        self._spam_thread       = None

        # F10 mutes/unmutes the beep entirely — starts unmuted.
        self.beep_hotkey  = beep_hotkey
        self.beep_enabled = True
        self.alert = RecordedAlert(_BASE_DIR / "detection_message.wav")
        self._audio_requests = queue.SimpleQueue()
        self._selection_requested = threading.Event()
        self._selector = None
        self._selecting = False
        self._region_revision = 0

    def _build_window(self):
        b = self.BORDER
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", "black")
        root.configure(bg="black")
        root.resizable(False, False)
        canvas = tk.Canvas(root, bg="black", highlightthickness=0)
        canvas.pack()
        self._rect_id = canvas.create_rectangle(0, 0, 0, 0, outline=self.COLOR,
                                                 width=b * 2, fill="black", tags="border")
        for corner in ("nw", "ne", "sw", "se"):
            hid = canvas.create_rectangle(0, 0, 0, 0, fill=self.COLOR, outline="",
                                           tags=("handle", f"handle_{corner}"))
            self._handle_ids[corner] = hid

        canvas.tag_bind("border", "<ButtonPress-1>",   self._on_move_press)
        canvas.tag_bind("border", "<B1-Motion>",       self._on_move_drag)
        canvas.tag_bind("border", "<ButtonRelease-1>", self._on_release)
        canvas.tag_bind("border", "<Double-Button-1>", lambda e: self._quit())
        for corner in ("nw", "ne", "sw", "se"):
            canvas.tag_bind(f"handle_{corner}", "<ButtonPress-1>",
                             lambda e, c=corner: self._on_resize_press(e, c))
            canvas.tag_bind(f"handle_{corner}", "<B1-Motion>",       self._on_resize_drag)
            canvas.tag_bind(f"handle_{corner}", "<ButtonRelease-1>", self._on_release)

        root.bind("<Escape>", lambda e: self._quit())

        self._canvas = canvas
        self.root    = root
        self._sync_geometry()
        self._exclude_from_capture()
        self._build_status_panel(root)
        return root

    def _build_status_panel(self, root):
        """A small always-on-top panel off to the side showing run status —
        separate from the box itself, which now stays a plain colored
        rectangle. Drag anywhere on the panel to reposition it."""
        panel = tk.Toplevel(root)
        panel.overrideredirect(True)
        panel.attributes("-topmost", True)
        panel.configure(bg="#1a1a1a")
        panel.geometry("240x104+20+20")

        self._panel_status = tk.Label(panel, text=self.status_text, fg=self.COLOR,
                                       bg="#1a1a1a", font=("Segoe UI", 13, "bold"),
                                       anchor="w")
        self._panel_status.pack(fill="x", padx=12, pady=(12, 6))
        self._panel_enter = tk.Label(panel, text="", fg="#AAAAAA", bg="#1a1a1a",
                                      font=("Segoe UI", 10), anchor="w")
        self._panel_enter.pack(fill="x", padx=12)
        self._panel_beep = tk.Label(panel, text="", fg="#AAAAAA", bg="#1a1a1a",
                                     font=("Segoe UI", 10), anchor="w")
        self._panel_beep.pack(fill="x", padx=12, pady=(0, 12))

        for widget in (panel, self._panel_status, self._panel_enter, self._panel_beep):
            widget.bind("<ButtonPress-1>", self._on_panel_drag_press)
            widget.bind("<B1-Motion>",     self._on_panel_drag_move)

        self._panel = panel

    def _on_panel_drag_press(self, event):
        self._panel_drag = (event.x_root, event.y_root,
                             self._panel.winfo_x(), self._panel.winfo_y())

    def _on_panel_drag_move(self, event):
        if not self._panel_drag:
            return
        sx, sy, ox, oy = self._panel_drag
        dx, dy = event.x_root - sx, event.y_root - sy
        self._panel.geometry(f"+{ox + dx}+{oy + dy}")

    def _exclude_from_capture(self):
        """Hide this overlay window from screen-capture APIs (mss included)
        so the box's own border/text never gets OCR'd as part of the region
        it's watching."""
        if platform.system() != "Windows":
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            WDA_EXCLUDEFROMCAPTURE = 0x11
            ok = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            if not ok:
                print("  [warning] Could not exclude overlay from screen capture — "
                      "the box's own border/text may interfere with OCR.", flush=True)
        except Exception as e:
            print(f"  [warning] Could not exclude overlay from screen capture: {e}", flush=True)

    def _sync_geometry(self):
        x, y, w, h = self.region
        b, hs = self.BORDER, self.HANDLE_SIZE
        W, H = w + b * 2, h + b * 2
        self.root.geometry(f"{W}x{H}+{x - b}+{y - b}")
        self._canvas.config(width=W, height=H)
        self._canvas.coords(self._rect_id, 0, 0, W - 1, H - 1)
        for corner, (hx, hy) in {"nw": (0, 0), "ne": (W, 0), "sw": (0, H), "se": (W, H)}.items():
            self._canvas.coords(self._handle_ids[corner], hx - hs, hy - hs, hx + hs, hy + hs)

    def _on_move_press(self, event):
        self._drag = {"mode": "move", "sx": event.x_root, "sy": event.y_root,
                       "orig": tuple(self.region)}

    def _on_move_drag(self, event):
        d = self._drag
        if d.get("mode") != "move":
            return
        dx, dy = event.x_root - d["sx"], event.y_root - d["sy"]
        ox, oy, w, h = d["orig"]
        self.region = [ox + dx, oy + dy, w, h]
        b = self.BORDER
        self.root.geometry(f"+{self.region[0] - b}+{self.region[1] - b}")

    def _on_resize_press(self, event, corner):
        self._drag = {"mode": "resize", "corner": corner, "sx": event.x_root, "sy": event.y_root,
                       "orig": tuple(self.region)}

    def _on_resize_drag(self, event):
        d = self._drag
        if d.get("mode") != "resize":
            return
        dx, dy = event.x_root - d["sx"], event.y_root - d["sy"]
        ox, oy, ow, oh = d["orig"]
        x1, y1, x2, y2 = ox, oy, ox + ow, oy + oh
        corner = d["corner"]
        if "n" in corner: y1 = min(y1 + dy, y2 - self.MIN_SIZE)
        if "s" in corner: y2 = max(y2 + dy, y1 + self.MIN_SIZE)
        if "w" in corner: x1 = min(x1 + dx, x2 - self.MIN_SIZE)
        if "e" in corner: x2 = max(x2 + dx, x1 + self.MIN_SIZE)
        self.region = [x1, y1, x2 - x1, y2 - y1]
        self._sync_geometry()

    def _on_release(self, _event):
        self._drag = {}
        save_region(tuple(self.region))

    def _start_selection(self):
        self._region_revision += 1
        self._selecting = True
        self._set_enter_on(False)
        self._unlock_mouse()
        self.hit = self._prev_hit = False
        self.last_value = None
        self.status_text = "Drag to select a region"
        self._drag = {}
        self.root.withdraw()
        self._panel.withdraw()
        if self._selector is not None:
            self._selector.reset()
        else:
            self._selector = RegionSelector(parent=self.root, on_done=self._finish_selection)

    def _finish_selection(self, region):
        if region is not None:
            self.region = list(region)
            save_region(region)
        self._selector = None
        self._region_revision += 1
        self.hit = self._prev_hit = False
        self.last_value = None
        self.status_text = "watching…"
        self._sync_geometry()
        self.root.deiconify()
        self._panel.deiconify()
        self._selecting = False

    def _quit(self):
        print("\nStopped.")
        self._running = False
        self._unlock_mouse()
        self.alert.close()
        hotkeys = ["f8", "f11", "f12", self.beep_hotkey, self.enter_hotkey]
        for hk in hotkeys:
            try:
                keyboard.remove_hotkey(hk)
            except (KeyError, ValueError):
                pass
        self.root.destroy()

    def _lock_mouse(self):
        """Pin the cursor to its current on-screen position (Windows only) so
        it can't drift off the click target while spam is running. Windows
        silently clears ClipCursor on focus changes — which happens
        constantly here since the game, not this overlay, is the foreground
        window while playing — so the spam loop calls _reassert_mouse_lock()
        on a short timer to keep re-applying the same rect."""
        if platform.system() != "Windows":
            return
        try:
            import ctypes
            from ctypes import wintypes
            pt = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            self._lock_rect = wintypes.RECT(pt.x, pt.y, pt.x + 1, pt.y + 1)
            ctypes.windll.user32.ClipCursor(ctypes.byref(self._lock_rect))
        except Exception as e:
            print(f"  [warning] Could not lock mouse position: {e}", flush=True)

    def _reassert_mouse_lock(self):
        """Re-apply the already-captured clip rect (does NOT re-read the
        current cursor position, so a reset-then-drift window can't shift
        the locked point)."""
        if platform.system() != "Windows" or self._lock_rect is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.ClipCursor(ctypes.byref(self._lock_rect))
        except Exception:
            pass

    def _unlock_mouse(self):
        if platform.system() != "Windows":
            return
        try:
            import ctypes
            ctypes.windll.user32.ClipCursor(None)
        except Exception:
            pass
        self._lock_rect = None

    def _set_enter_on(self, value: bool):
        """Single place that flips enter_on so the mouse lock always tracks
        it, no matter which of the several call sites (hotkey, auto-stop on
        detection, false-positive resume) changes the state."""
        if value and self._selecting:
            return
        if value == self.enter_on:
            return
        self.enter_on = value
        self._activation += 1
        if value and self.enter_spam_enabled:
            self._lock_mouse()
        else:
            self._unlock_mouse()

    def _toggle_enter_spam(self):
        """Toggle OCR and automation. Start each activation with a fresh read."""
        self.hit = False
        self._prev_hit = False
        self.last_value = None
        self._set_enter_on(not self.enter_on)
        self.status_text = "watching…" if self.enter_on else "paused"
        print(f"\n  [hotkey {self.enter_hotkey.upper()}] Enter+Left-Click spam "
              f"{'ON' if self.enter_on else 'OFF'}\n", flush=True)

    def _toggle_beep(self):
        """Hotkey callback — mutes/unmutes the detection beep, independent of
        the Enter+Click spam state."""
        self.beep_enabled = not self.beep_enabled
        print(f"\n  [hotkey {self.beep_hotkey.upper()}] Beep "
              f"{'UNMUTED' if self.beep_enabled else 'MUTED'}\n", flush=True)

    def _play_alert(self):
        self.alert.play(beep)

    @staticmethod
    def _jittered(interval, spread=SPAM_JITTER):
        """Randomize *interval* by +/- spread so spammed presses/clicks don't
        land on a perfectly even, obviously-scripted cadence."""
        return interval * random.uniform(1 - spread, 1 + spread)

    def _spam_loop(self):
        """
        Runs on its own thread so the millisecond-scale Enter/click cadence
        isn't tied to the (much coarser) OCR poll interval. pydirectinput is
        built specifically for games that read DirectInput device state rather
        than the Windows message queue or global keyboard hooks — both
        pyautogui's VK press and keyboard's scan-code SendInput can be invisible
        to those games. Enter and click run on independent cadences, each
        re-randomized after every press so the gap itself varies over time
        rather than just being offset by a fixed amount.
        """
        last_enter_time = 0.0
        last_click_time = 0.0
        last_lock_refresh = 0.0
        next_enter_gap = self._jittered(self.enter_interval)
        next_click_gap = self._jittered(self.click_interval)
        while self._running:
            if self.enter_on and not self.hit and not self._selecting:
                now = time.perf_counter()
                # Re-assert the mouse lock a few times a second — Windows
                # clears ClipCursor on focus changes, which happen
                # continuously while the game (not this overlay) holds
                # foreground focus during actual play.
                if now - last_lock_refresh >= 0.05:
                    self._reassert_mouse_lock()
                    last_lock_refresh = now
                if now - last_enter_time >= next_enter_gap:
                    pydirectinput.press("enter")
                    last_enter_time = now
                    next_enter_gap = self._jittered(self.enter_interval)
                if now - last_click_time >= next_click_gap:
                    pydirectinput.click()
                    last_click_time = now
                    next_click_gap = self._jittered(self.click_interval)
            time.sleep(0.001)

    def _read_with_retry(self, sct):
        try:
            return read_delta(tuple(self.region), sct=sct)
        except Exception as e:
            print(f"  [ocr error] {e}", flush=True)
            return None

    def _ocr_loop(self):
        """
        Runs on its own thread, back-to-back with no artificial delay (unless
        min_read_gap > 0) — decoupled from tkinter's event loop entirely, so
        the UI never blocks on a screenshot+OCR call and the next read starts
        the instant the previous one finishes. Only touches plain attributes
        (never the canvas directly — Tk calls must stay on the main thread);
        _render() picks those up on its own tick.
        """
        with mss.MSS() as sct:
            while self._running:
                if self._selecting or not self.enter_on:
                    time.sleep(RENDER_INTERVAL)
                    continue
                revision = self._region_revision
                activation = self._activation
                t0 = time.perf_counter()
                value = self._read_with_retry(sct)
                if (self._selecting or revision != self._region_revision
                        or not self.enter_on or activation != self._activation):
                    continue

                if value and not self._prev_hit:
                    # New detection (edge-triggered, so a popup that stays on
                    # screen for several reads doesn't re-trigger this every
                    # frame). Stop the spam immediately so it doesn't click
                    # through the popup, then take one more independent read
                    # before believing it — a single stray OCR frame was
                    # producing occasional false positives, so only a second
                    # confirming read earns the beep.
                    self.hit = True
                    self._unlock_mouse()
                    self.status_text = "confirming…"

                    confirm = self._read_with_retry(sct)
                    if (self._selecting or revision != self._region_revision
                            or not self.enter_on or activation != self._activation):
                        continue
                    if confirm:
                        value = confirm
                        self.status_text = value
                        self.last_value = value
                        print(f"[{time.strftime('%H:%M:%S')}] detected: {value}", flush=True)
                        if self.beep_enabled:
                            now = time.perf_counter()
                            if now - self.last_beep_time >= BEEP_COOLDOWN:
                                threading.Thread(target=self._play_alert, daemon=True).start()
                                self.last_beep_time = now
                        self._set_enter_on(False)
                        print(f"  [detection] OFF — detected {value}. "
                              f"Press {self.enter_hotkey.upper()} to re-enable.\n", flush=True)
                    else:
                        print("  [ocr] ignored a one-frame false positive.", flush=True)
                        value = None
                        self.hit = False
                        self.status_text = "watching…"
                        self.last_value = None
                        if self.enter_spam_enabled:
                            self._lock_mouse()
                elif value:
                    self.hit = True
                    self.status_text = value
                    self.last_value = value
                else:
                    self.hit = False
                    self.status_text = "watching…"
                    self.last_value = None

                self._prev_hit = self.hit

                if self.min_read_gap > 0:
                    remaining = self.min_read_gap - (time.perf_counter() - t0)
                    if remaining > 0:
                        time.sleep(remaining)

    def _render(self):
        """Fast, OCR-free UI repaint tick — just reflects whatever state the
        background OCR loop (or the spam loop) last wrote."""
        if self._selection_requested.is_set():
            self._selection_requested.clear()
            self._start_selection()
        # Keep microphone operations outside the Windows keyboard hook.
        try:
            action = self._audio_requests.get_nowait()
        except queue.Empty:
            pass
        else:
            action()
        color = self.COLOR_HIT if self.hit else self.COLOR
        if self._canvas:
            self._canvas.itemconfig("border", outline=color)
            for hid in self._handle_ids.values():
                self._canvas.itemconfig(hid, fill=color)
        if self._panel_status:
            self._panel_status.config(text=self.status_text, fg=color)
            if self.enter_spam_enabled:
                self._panel_enter.config(text=f"Enter+Click: {'ON' if self.enter_on else 'OFF'}")
                self._panel_beep.config(text=f"Beep: {'ON' if self.beep_enabled else 'MUTED'}")
        self.root.after(int(RENDER_INTERVAL * 1000), self._render)

    def run(self):
        root = self._build_window()
        keyboard.add_hotkey("f8", self._selection_requested.set,
                            suppress=True, trigger_on_release=True)
        print("[hotkey] F8: draw a new region; Escape: cancel selection.", flush=True)
        keyboard.add_hotkey("f11", self._audio_requests.put, args=(self.alert.toggle,),
                            suppress=True, trigger_on_release=True)
        keyboard.add_hotkey("f12", self._audio_requests.put, args=(self.alert.reset,),
                            suppress=True, trigger_on_release=True)
        keyboard.add_hotkey(self.beep_hotkey, self._toggle_beep)
        keyboard.add_hotkey(self.enter_hotkey, self._toggle_enter_spam)
        print("[hotkey] F11: start recording; F11 again: save message. "
              "Repeat to replace it. See [audio] messages for recording status.", flush=True)
        print("[hotkey] F12: delete message and restore beep (mute unchanged).", flush=True)
        print(f"[hotkey] {self.beep_hotkey.upper()}: mute/unmute detection audio.", flush=True)
        if self.enter_spam_enabled:
            print(f"[hotkey] Press {self.enter_hotkey.upper()} to start/stop Enter+Left-Click spam "
                  f"(starts OFF — Enter every {self.enter_interval*1000:.0f}ms, "
                  f"click every {self.click_interval*1000:.0f}ms while nothing is detected; "
                  f"stops automatically — with one beep — the instant a '+<number>' is detected).\n",
                  flush=True)
            self._spam_thread = threading.Thread(target=self._spam_loop, daemon=True)
            self._spam_thread.start()
        self._ocr_thread = threading.Thread(target=self._ocr_loop, daemon=True)
        self._ocr_thread.start()
        root.after(int(RENDER_INTERVAL * 1000), self._render)
        try:
            root.mainloop()
        except KeyboardInterrupt:
            self._quit()

# ── Visual region selector (initial placement) ──────────────────────────────

BORDER_COLOR = "#00FF88"


class RegionSelector:
    MIN_SIZE = 20

    def __init__(self, parent=None, on_done=None):
        self.result = None
        self.on_done = on_done
        self.root = tk.Toplevel(parent) if parent else tk.Tk()
        self.root.title("Select Region")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        with mss.MSS() as sct:
            desktop = sct.monitors[0]
        self.left, self.top = desktop["left"], desktop["top"]
        sw, sh = desktop["width"], desktop["height"]
        self.root.geometry(f"{sw}x{sh}+{self.left}+{self.top}")
        # Alpha keeps the desktop visible without making the canvas click-through.
        self.root.attributes("-alpha", 0.3)
        self.root.configure(bg="black")

        self.canvas = tk.Canvas(self.root, bg="black",
                                 highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self._info_font = tkfont.Font(root=self.root, family="Courier", size=11, weight="bold")
        self._bind_events()
        if parent is None:
            self.root.bind("<KeyRelease-F8>", lambda e: self.reset())
        self.reset()

    def reset(self):
        self.rx1 = self.ry1 = self.rx2 = self.ry2 = 0
        self._drag_data = {}
        self.canvas.delete("all")
        self.canvas.create_text(24, 24, anchor="nw", fill="white", font=self._info_font,
                                text="Drag to select | F8: restart | Esc: cancel")
        self.root.lift()
        self.root.focus_force()

    def _draw(self):
        c = self.canvas
        x1, y1, x2, y2 = self.rx1, self.ry1, self.rx2, self.ry2
        c.delete("all")
        c.create_rectangle(x1, y1, x2, y2, outline=BORDER_COLOR, width=2)
        w, h = x2 - x1, y2 - y1
        label = f"  {w}x{h}px  |  Release to select  |  F8=Restart  Esc=Cancel  "
        lx = x1 + w // 2
        ly = y1 - 18 if y1 > 30 else y2 + 18
        c.create_text(lx + 1, ly + 1, text=label, font=self._info_font, fill="black", anchor="center")
        c.create_text(lx,     ly,     text=label, font=self._info_font, fill=BORDER_COLOR, anchor="center")

    def _bind_events(self):
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>",   lambda e: self._quit())

    def _on_press(self, event):
        ex, ey = event.x, event.y
        self._drag_data = {"sx": ex, "sy": ey}
        self.rx1 = self.rx2 = ex
        self.ry1 = self.ry2 = ey

    def _on_drag(self, event):
        ex, ey = event.x, event.y
        d = self._drag_data
        if not d:
            return
        ex = max(0, min(ex, self.canvas.winfo_width()))
        ey = max(0, min(ey, self.canvas.winfo_height()))
        self.rx1, self.rx2 = sorted((d["sx"], ex))
        self.ry1, self.ry2 = sorted((d["sy"], ey))
        self._draw()

    def _on_release(self, event):
        if not self._drag_data:
            return
        self._on_drag(event)
        self._drag_data = {}
        self._confirm()

    def _confirm(self):
        x1, y1 = min(self.rx1, self.rx2), min(self.ry1, self.ry2)
        x2, y2 = max(self.rx1, self.rx2), max(self.ry1, self.ry2)
        if x2 - x1 < self.MIN_SIZE or y2 - y1 < self.MIN_SIZE:
            self.reset()
            return
        self.result = (x1 + self.left, y1 + self.top, x2 - x1, y2 - y1)
        self.root.destroy()
        if self.on_done:
            self.on_done(self.result)

    def _quit(self):
        self.root.destroy()
        if self.on_done:
            self.on_done(None)

    def run(self) -> tuple:
        self.root.mainloop()
        return self.result

# ── Entry point ──────────────────────────────────────────────────────────────

def _open_selector() -> tuple:
    print("  • Click-drag to select a region; release to confirm")
    print("  • Press F8 to clear the selection and draw again")
    print("  • Resize the green box afterward with its corner handles")
    print("  • Press Escape to quit")
    print()
    sel = RegionSelector()
    region = sel.run()
    if region is None:
        print("No region selected.")
        sys.exit(0)
    save_region(region)
    print(f"  Region saved: {region}")
    return region


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Green OCR box — reads '+<number>' deltas inside it.")
    parser.add_argument("--region", nargs=4, type=int, metavar=("L", "T", "W", "H"))
    parser.add_argument("--once", action="store_true", help="read once and print the result, no overlay")
    parser.add_argument("--interval", type=float, default=MIN_READ_GAP,
                         help="extra delay in seconds between OCR reads on top of screenshot+OCR time "
                              "(default: 0.0 = back-to-back, as fast as possible)")
    parser.add_argument("--reselect", action="store_true", help="re-open the region selector even if a region is saved")
    parser.add_argument("--no-enter-spam", action="store_true", help="disable the F9 Enter+Left-Click spam feature entirely (no hotkey, no presses/clicks)")
    parser.add_argument("--enter-interval", type=float, default=ENTER_INTERVAL, help="seconds between spammed Enter presses (default: 0.16 = 160ms)")
    parser.add_argument("--click-interval", type=float, default=CLICK_INTERVAL, help="seconds between spammed left-clicks (default: 0.24 = 240ms)")
    parser.add_argument("--enter-hotkey", default=ENTER_HOTKEY, help="global Enter-spam on/off hotkey (default: f9)")
    parser.add_argument("--beep-hotkey", default=BEEP_HOTKEY, help="global beep mute/unmute hotkey (default: f10)")
    parser.add_argument("--elevate", action="store_true",
                         help="re-launch this script as Administrator (fixes F9 not responding "
                              "while an elevated game window has focus) — on by default, see --no-elevate")
    parser.add_argument("--no-elevate", action="store_true",
                         help="skip the automatic UAC elevation prompt on startup")
    args = parser.parse_args()

    # Auto-elevate by default so F9 keeps working even while an elevated game
    # window has focus, with no manual step (opening as admin, etc.) needed.
    if platform.system() == "Windows" and not args.no_elevate and not _is_admin():
        print("  Not running as Administrator — re-launching elevated (UAC prompt incoming)...")
        print("  (pass --no-elevate to skip this)")
        _relaunch_as_admin()

    if args.region:
        region = tuple(args.region)
        save_region(region)
    else:
        region = load_region()

    if region is None or args.reselect:
        print("  Opening region selector...")
        print()
        region = _open_selector()
    else:
        print(f"  Last region: left={region[0]}  top={region[1]}  width={region[2]}  height={region[3]}")

    if args.once:
        value = read_delta(region)
        print(f"detected: {value}" if value else "no '+<number>' found")
        sys.exit(0 if value else 1)

    print(f"\n  Watching region={region} — drag the box to reposition, drag a corner to resize.")
    print("  Double-click the border or press Escape to quit.\n")
    OverlayApp(region, enter_spam=not args.no_enter_spam,
               enter_hotkey=args.enter_hotkey,
               enter_interval=args.enter_interval,
               click_interval=args.click_interval,
               beep_hotkey=args.beep_hotkey,
               min_read_gap=args.interval).run()
