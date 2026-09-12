#!/usr/bin/env python3
"""
Draggable/resizable green box overlay that OCRs the "+<number>" delta inside
it — e.g. "+538,683" from a "Combat Power Change" popup (see +delata_ocr.png:
bold white text with a black outline on a dark green/olive panel).

Position the box over the number, and it's continuously screenshotted,
preprocessed (upscaled + grayscaled), and OCR'd with Tesseract. Whenever a
"+<digits>" match appears, it's shown in the box and printed to the console;
a beep fires once per new value (not every frame the popup happens to still
be on screen). The OCR loop runs on its own background thread, back-to-back
with no artificial delay between reads, so detection latency is bounded only
by how fast screen-capture + Tesseract actually are (~150ms/read) instead of
also waiting out a fixed poll interval on top of that.

Press F9 (global — works even while the game has focus) to start spamming
Enter + left-click. It keeps spamming until the box detects a "+<number>",
at which point it beeps once and stops spamming on its own — press F9 again
to resume.

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
import platform
import threading
import tkinter as tk
from tkinter import font as tkfont
from pathlib import Path

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
MIN_READ_GAP    = 0.0    # optional extra delay between OCR reads (0 = back-to-back, as fast as possible)
RENDER_INTERVAL = 0.05   # seconds between UI repaints — independent of the OCR read cadence
OCR_SCALE       = 3      # upscale factor before OCR — small popup text needs this
BEEP_COOLDOWN   = 1.5    # min seconds between beeps

ENTER_HOTKEY    = "f9"    # toggles Enter+Left-Click spam on/off — starts OFF
ENTER_INTERVAL  = 0.16    # seconds between spammed Enter presses (160ms)
CLICK_INTERVAL  = 0.24    # seconds between spammed left-clicks (240ms)
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
    """Upscale + grayscale — small popup text needs the extra resolution."""
    big = img.resize((img.width * OCR_SCALE, img.height * OCR_SCALE), Image.LANCZOS)
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
    A green, always-on-top, click-through-in-the-middle box: only its border
    and the four corner handles receive mouse events, so drag the border to
    move it, drag a corner to resize it. Runs its own OCR poll loop via
    root.after() ticks on the main thread (no background thread needed).
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
        self.status_text = "watching…"
        self.hit          = False
        self._prev_hit    = False   # edge-detect found/not-found so beep+stop fires once per detection
        self.last_value    = None
        self.last_beep_time = 0.0
        self.min_read_gap  = min_read_gap
        self.root         = None
        self._canvas      = None
        self._rect_id     = None
        self._text_id     = None
        self._handle_ids  = {}
        self._drag        = {}
        self._ocr_thread  = None

        # Enter+Left-Click spam — starts OFF, F9 toggles it on; a detection
        # beeps once (unless muted) and turns it back off until F9 is pressed
        # again. The beep only ever fires while spam was actively running —
        # a detection while idle (F9 off) is silent.
        self.enter_spam_enabled = enter_spam
        self.enter_hotkey       = enter_hotkey
        self.enter_interval     = enter_interval
        self.click_interval     = click_interval
        self.enter_on           = False
        self._running           = True
        self._spam_thread       = None

        # F10 mutes/unmutes the beep entirely — starts unmuted.
        self.beep_hotkey  = beep_hotkey
        self.beep_enabled = True

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
        self._text_id = canvas.create_text(0, 0, text=self.status_text, anchor="nw",
                                            fill=self.COLOR, font=("Segoe UI", 11, "bold"),
                                            width=500, tags="status")
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
        return root

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
        self._canvas.coords(self._text_id, b + 4, b + 4)
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

    def _quit(self):
        print("\nStopped.")
        self._running = False
        if self.enter_spam_enabled:
            for hk in (self.enter_hotkey, self.beep_hotkey):
                try:
                    keyboard.remove_hotkey(hk)
                except (KeyError, ValueError):
                    pass
        self.root.destroy()

    def _toggle_enter_spam(self):
        """Hotkey callback (runs on keyboard's own thread) — just flips a flag;
        the spam thread and the UI poll both pick it up on their next tick."""
        self.enter_on = not self.enter_on
        print(f"\n  [hotkey {self.enter_hotkey.upper()}] Enter+Left-Click spam "
              f"{'ON' if self.enter_on else 'OFF'}\n", flush=True)

    def _toggle_beep(self):
        """Hotkey callback — mutes/unmutes the detection beep, independent of
        the Enter+Click spam state."""
        self.beep_enabled = not self.beep_enabled
        print(f"\n  [hotkey {self.beep_hotkey.upper()}] Beep "
              f"{'UNMUTED' if self.beep_enabled else 'MUTED'}\n", flush=True)

    def _spam_loop(self):
        """
        Runs on its own thread so the millisecond-scale Enter/click cadence
        isn't tied to the (much coarser) OCR poll interval. pydirectinput is
        built specifically for games that read DirectInput device state rather
        than the Windows message queue or global keyboard hooks — both
        pyautogui's VK press and keyboard's scan-code SendInput can be invisible
        to those games. Enter and click run on independent cadences.
        """
        last_enter_time = 0.0
        last_click_time = 0.0
        while self._running:
            if self.enter_on and not self.hit:
                now = time.perf_counter()
                if now - last_enter_time >= self.enter_interval:
                    pydirectinput.press("enter")
                    last_enter_time = now
                if now - last_click_time >= self.click_interval:
                    pydirectinput.click()
                    last_click_time = now
            time.sleep(0.001)

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
                t0 = time.perf_counter()
                try:
                    value = read_delta(tuple(self.region), sct=sct)
                except Exception as e:
                    value = None
                    print(f"  [ocr error] {e}", flush=True)

                if value:
                    self.hit = True
                    self.status_text = value
                    print(f"[{time.strftime('%H:%M:%S')}] detected: {value}", flush=True)
                    self.last_value = value
                else:
                    self.hit = False
                    self.status_text = "watching…"
                    self.last_value = None

                # Edge-triggered: fire only on the transition into a detection,
                # so a popup that stays on screen for several reads only
                # beeps/stops once. The beep only fires when spam was actually
                # running (self.enter_on) — a detection while idle (F9 off)
                # is silent, per request.
                if self.hit and not self._prev_hit:
                    if self.enter_on:
                        if self.beep_enabled:
                            now = time.perf_counter()
                            if now - self.last_beep_time >= BEEP_COOLDOWN:
                                threading.Thread(target=beep, daemon=True).start()
                                self.last_beep_time = now
                        self.enter_on = False
                        print(f"  [enter+click] OFF — detected {value}. "
                              f"Press {self.enter_hotkey.upper()} to re-enable.\n", flush=True)
                self._prev_hit = self.hit

                if self.min_read_gap > 0:
                    remaining = self.min_read_gap - (time.perf_counter() - t0)
                    if remaining > 0:
                        time.sleep(remaining)

    def _render(self):
        """Fast, OCR-free UI repaint tick — just reflects whatever state the
        background OCR loop (or the spam loop) last wrote."""
        if self._canvas:
            color = self.COLOR_HIT if self.hit else self.COLOR
            self._canvas.itemconfig("border", outline=color)
            text = self.status_text
            if self.enter_spam_enabled:
                text += f"  [Enter+Click: {'ON' if self.enter_on else 'OFF'}]"
                text += f"  [Beep: {'ON' if self.beep_enabled else 'MUTED'}]"
            self._canvas.itemconfig("status", text=text, fill=color)
            for hid in self._handle_ids.values():
                self._canvas.itemconfig(hid, fill=color)
        self.root.after(int(RENDER_INTERVAL * 1000), self._render)

    def run(self):
        root = self._build_window()
        if self.enter_spam_enabled:
            keyboard.add_hotkey(self.enter_hotkey, self._toggle_enter_spam)
            print(f"[hotkey] Press {self.enter_hotkey.upper()} to start/stop Enter+Left-Click spam "
                  f"(starts OFF — Enter every {self.enter_interval*1000:.0f}ms, "
                  f"click every {self.click_interval*1000:.0f}ms while nothing is detected; "
                  f"stops automatically — with one beep — the instant a '+<number>' is detected).\n",
                  flush=True)
            keyboard.add_hotkey(self.beep_hotkey, self._toggle_beep)
            print(f"[hotkey] Press {self.beep_hotkey.upper()} to mute/unmute the detection beep "
                  f"(starts unmuted).\n", flush=True)
            self._spam_thread = threading.Thread(target=self._spam_loop, daemon=True)
            self._spam_thread.start()
        self._ocr_thread = threading.Thread(target=self._ocr_loop, daemon=True)
        self._ocr_thread.start()
        root.after(int(RENDER_INTERVAL * 1000), self._render)
        try:
            root.mainloop()
        except KeyboardInterrupt:
            print("\nStopped.")
            self._running = False

# ── Visual region selector (initial placement) ──────────────────────────────

HANDLE_SIZE  = 10
BORDER_COLOR = "#00FF88"
HANDLE_COLOR = "#FFFFFF"
DIM_COLOR    = "#000000"


class RegionSelector:
    MIN_SIZE = 20

    def __init__(self, initial_region=None):
        self.result = None

        self.root = tk.Tk()
        self.root.title("Select Region")
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        if platform.system() == "Windows":
            self.root.attributes("-transparentcolor", "black")
            self.root.configure(bg="black")
        elif platform.system() == "Darwin":
            self.root.attributes("-transparent", True)
            self.root.configure(bg="systemTransparent")
        else:
            self.root.attributes("-alpha", 0.85)
            self.root.configure(bg="#1a1a1a")

        self.canvas = tk.Canvas(self.root, bg="black",
                                 highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        if initial_region:
            lx, ty, rw, rh = initial_region
            self.rx1, self.ry1 = lx, ty
            self.rx2, self.ry2 = lx + rw, ty + rh
        else:
            cx, cy = sw // 2, sh // 2
            self.rx1, self.ry1 = cx - 200, cy - 60
            self.rx2, self.ry2 = cx + 200, cy + 60

        self._drag_data = {}
        self._info_font = tkfont.Font(family="Courier", size=11, weight="bold")
        self._bind_events()
        self._draw()

    def _draw(self):
        c = self.canvas
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x1, y1, x2, y2 = self.rx1, self.ry1, self.rx2, self.ry2
        c.delete("all")
        c.create_rectangle(0,  0,  sw, y1, fill=DIM_COLOR, outline="")
        c.create_rectangle(0,  y2, sw, sh, fill=DIM_COLOR, outline="")
        c.create_rectangle(0,  y1, x1, y2, fill=DIM_COLOR, outline="")
        c.create_rectangle(x2, y1, sw, y2, fill=DIM_COLOR, outline="")
        c.create_rectangle(x1, y1, x2, y2, outline=BORDER_COLOR, width=2)
        for hx, hy, _ in self._handle_positions():
            c.create_rectangle(hx - HANDLE_SIZE, hy - HANDLE_SIZE,
                                hx + HANDLE_SIZE, hy + HANDLE_SIZE,
                                fill=HANDLE_COLOR, outline=BORDER_COLOR, width=1)
        w, h = x2 - x1, y2 - y1
        label = f"  {w}x{h}px  |  ({x1},{y1})  |  Enter=Confirm  Esc=Quit  "
        lx = x1 + w // 2
        ly = y1 - 18 if y1 > 30 else y2 + 18
        c.create_text(lx + 1, ly + 1, text=label, font=self._info_font, fill="black", anchor="center")
        c.create_text(lx,     ly,     text=label, font=self._info_font, fill=BORDER_COLOR, anchor="center")

    def _handle_positions(self):
        x1, y1, x2, y2 = self.rx1, self.ry1, self.rx2, self.ry2
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2
        return [(x1, y1, "h_nw"), (mx, y1, "h_n"), (x2, y1, "h_ne"),
                (x1, my, "h_w"),                    (x2, my, "h_e"),
                (x1, y2, "h_sw"), (mx, y2, "h_s"), (x2, y2, "h_se")]

    def _bind_events(self):
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", lambda e: self._confirm())
        self.root.bind("<Return>",   lambda e: self._confirm())
        self.root.bind("<KP_Enter>", lambda e: self._confirm())
        self.root.bind("<Escape>",   lambda e: self._quit())

    def _hit_handle(self, ex, ey):
        for hx, hy, tag in self._handle_positions():
            if hx - HANDLE_SIZE <= ex <= hx + HANDLE_SIZE and hy - HANDLE_SIZE <= ey <= hy + HANDLE_SIZE:
                return tag
        return None

    def _on_press(self, event):
        ex, ey = event.x, event.y
        handle = self._hit_handle(ex, ey)
        if handle:
            self._drag_data = {"mode": "resize", "handle": handle}
        elif self.rx1 <= ex <= self.rx2 and self.ry1 <= ey <= self.ry2:
            self._drag_data = {"mode": "move", "ox": ex - self.rx1, "oy": ey - self.ry1}
        else:
            self._drag_data = {}

    def _on_drag(self, event):
        ex, ey = event.x, event.y
        d = self._drag_data
        if not d:
            return
        if d["mode"] == "move":
            w, h = self.rx2 - self.rx1, self.ry2 - self.ry1
            self.rx1, self.ry1 = ex - d["ox"], ey - d["oy"]
            self.rx2, self.ry2 = self.rx1 + w, self.ry1 + h
        elif d["mode"] == "resize":
            tag = d["handle"]
            if "w" in tag: self.rx1 = min(ex, self.rx2 - self.MIN_SIZE)
            if "e" in tag: self.rx2 = max(ex, self.rx1 + self.MIN_SIZE)
            if "n" in tag: self.ry1 = min(ey, self.ry2 - self.MIN_SIZE)
            if "s" in tag: self.ry2 = max(ey, self.ry1 + self.MIN_SIZE)
        self._draw()

    def _on_release(self, _): self._drag_data = {}

    def _confirm(self):
        x1, y1 = min(self.rx1, self.rx2), min(self.ry1, self.ry2)
        x2, y2 = max(self.rx1, self.rx2), max(self.ry1, self.ry2)
        self.result = (x1, y1, x2 - x1, y2 - y1)
        self.root.destroy()

    def _quit(self):
        self.root.destroy()
        print("Cancelled.")
        sys.exit(0)

    def run(self) -> tuple:
        self.root.mainloop()
        return self.result

# ── Entry point ──────────────────────────────────────────────────────────────

def _open_selector(initial_region=None) -> tuple:
    print("  • Drag inside the box to move it")
    print("  • Drag corners/edges to resize")
    print("  • Press Enter or double-click to confirm")
    print("  • Press Escape to quit")
    print()
    sel = RegionSelector(initial_region=initial_region)
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
        region = _open_selector(initial_region=region)
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
