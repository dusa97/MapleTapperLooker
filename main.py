#!/usr/bin/env python3
"""
Detects if "Skill Cooldowns -2 sec" appears on 2 separate lines in a screen region.

Strategy
--------
  Cold start  : uses Tesseract OCR to find the text and extract a pixel template
  Warm (fast) : uses OpenCV template matching (~2-5ms vs ~200ms for OCR)

The template is auto-saved to disk so it persists across restarts.

Run without arguments to open the visual region selector.

Requirements:
    pip install pillow pytesseract pyautogui opencv-python numpy
Tesseract OCR engine:
    - Windows : https://github.com/UB-Mannheim/tesseract/wiki
    - macOS   : brew install tesseract
    - Linux   : sudo apt install tesseract-ocr
"""

import time, sys, os, shutil, platform, threading, tkinter as tk
from tkinter import font as tkfont
from pathlib import Path
from collections import defaultdict

try:
    import pyautogui
    import pytesseract
    from PIL import Image
    import numpy as np
    import cv2
    import keyboard        # global hotkey to pause/resume — works even while the game has focus
    import pydirectinput   # DirectInput-style key injection — see the Enter-spam comment in watch()
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install pillow pytesseract pyautogui opencv-python numpy keyboard pydirectinput")
    sys.exit(1)

pydirectinput.FAILSAFE = False   # don't abort if the mouse happens to be at a screen corner
pydirectinput.PAUSE    = 0       # no built-in delay after every press()/click() call —
                                  # our own enter_interval/click_interval control the cadence instead

# The `tesseract` binary (not just the pytesseract wrapper) is required — if it's
# not yet on PATH (e.g. right after installing it, before a new shell picks up
# the PATH change), fall back to the standard Windows install location.
if platform.system() == "Windows" and not shutil.which("tesseract"):
    _default_tesseract = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if _default_tesseract.exists():
        pytesseract.pytesseract.tesseract_cmd = str(_default_tesseract)

# ── Elevation (Windows) ──────────────────────────────────────────────────────────
# keyboard.add_hotkey() installs a real global low-level hook — it does NOT need
# PyCharm (or any window) focused. The one thing that DOES break that: if the game
# itself runs elevated (as Administrator — common with anti-cheat) and this script
# does not, Windows' UIPI blocks a lower-privilege hook from seeing keystrokes while
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
    """Re-launch this same script elevated (triggers a UAC prompt) and exit."""
    import ctypes
    params = " ".join(
        f'"{a}"' if " " in a else a for a in sys.argv[1:] if a != "--elevate"
    )
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{__file__}" {params}', None, 1)
    sys.exit(0)

# ── Available detection profiles ──────────────────────────────────────────────
# Add as many profiles here as you like.
# "template" is the filename of the saved template image (auto-built by OCR if missing).
# "count"    is how many stacked lines must be present to trigger the alert.

PROFILES = {
    "1": {
        "name":      "Attack Power",
        "target":    "Attack Power",
        "template":  "attack_power_template.png",
        "count":     3,
        "threshold": 0.75,   # lenient — font varies less between variants
    },
    "2": {
        "name":      "Skill Cooldowns -2 sec",
        "target":    "Skill Cooldowns -2 sec",
        "template":  ["skill_cd_template.png", "skill_cd_template2.png"],
        "count":     2,
        "threshold": 0.90,   # -2 sec scores 1.000, -1 sec scores ~0.81 (template is just "-2 sec")
    },
    # Combat Power Change: the label ("Combat Power Change") is pixel-identical
    # every time, so it's matched the normal template way. The number under it is
    # different every time so it can't be template-matched — instead, once the
    # label matches, "classify_sign" samples the pixel brightness of the number
    # region: a "+" delta renders near-white, a "-" delta never gets that bright
    # (confirmed on real samples: +538,683 had a 6.25% bright-pixel fraction in
    # the number band, -2,453,883 had 0%). Only "+" counts as found -> beeps;
    # "-" is read but treated as not-found, so it's a no-op.
    "3": {
        "name":         "Combat Power Change",
        "target":       "Combat Power",
        "template":     "combat_power_template.png",
        "count":        1,
        "threshold":    0.7,
        "classify_sign": True,
    },
}

# Used when --profile isn't passed on the command line, so a bare run needs no input.
DEFAULT_PROFILE = "3"

# ── Runtime config (populated at startup from chosen profile) ──────────────────

TARGET_TEXT     = ""
REQUIRED_COUNT  = 1
POLL_INTERVAL   = 0.0
CLASSIFY_SIGN   = False   # profile-specific: classify a match as +/- by pixel brightness

# Fraction of "bright" (>BRIGHT_LEVEL) pixels in the number region needed to
# call a match a "+" gain rather than a "-" loss. Calibrated on real samples:
# a "+" delta scored 0.0625, a "-" delta scored 0.0 — 0.02 leaves wide margin.
BRIGHT_LEVEL     = 200
BRIGHT_FRACTION  = 0.02

TEMPLATE_FILE   = Path(__file__).parent / "attack_power_template.png"
REGION_FILE     = Path(__file__).parent / "last_region.json"
MATCH_THRESHOLD = 0.75
MAX_LINE_SPACING_FACTOR = 2.5
OCR_SCALE       = 2
OCR_CONFIG      = "--psm 11 --oem 1"

# ── Pause/resume + Enter-spam toggle hotkeys ────────────────────────────────────
# Both global (work even while the game window has focus, not just the overlay).

HOTKEY        = "f6"   # pauses/resumes detection + Enter-spam together
ENTER_HOTKEY  = "f9"   # toggles Enter-spam on its own, independent of pause
_paused       = False
_enter_on     = False  # Enter-spam starts OFF — must be turned on with ENTER_HOTKEY

def _toggle_pause():
    global _paused
    _paused = not _paused
    print(f"\n  [hotkey {HOTKEY.upper()}] {'PAUSED' if _paused else 'RESUMED'}\n", flush=True)

def _toggle_enter_spam():
    global _enter_on
    _enter_on = not _enter_on
    print(f"\n  [hotkey {ENTER_HOTKEY.upper()}] Enter+Left-Click spam {'ON' if _enter_on else 'OFF'}\n", flush=True)

# ── Beep ───────────────────────────────────────────────────────────────────────

def beep():
    system = platform.system()
    if system == "Windows":
        import winsound; winsound.Beep(1000, 300)
    elif system == "Darwin":
        os.system("afplay /System/Library/Sounds/Ping.aiff")
    else:
        if os.system("paplay /usr/share/sounds/freedesktop/stereo/bell.oga 2>/dev/null") != 0:
            print("\a", end="", flush=True)

# ── Template management ────────────────────────────────────────────────────────

_templates: list = []   # list of np.ndarray, one per template file

def _load_templates() -> list:
    """Load all template files for the active profile. Returns list of arrays."""
    files = TEMPLATE_FILE if isinstance(TEMPLATE_FILE, list) else [TEMPLATE_FILE]
    loaded = []
    for f in files:
        if Path(f).exists():
            t = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
            if t is not None:
                loaded.append(t)
                print(f"[template] Loaded {Path(f).name} ({t.shape[1]}x{t.shape[0]}px)", flush=True)
    return loaded


def _save_template(arr: np.ndarray):
    # Save to first template file in list
    f = TEMPLATE_FILE[0] if isinstance(TEMPLATE_FILE, list) else TEMPLATE_FILE
    cv2.imwrite(str(f), arr)
    print(f"  [template] saved to {f} ({arr.shape[1]}x{arr.shape[0]}px)", flush=True)


def _extract_template_from_ocr(img: Image.Image) -> np.ndarray | None:
    """
    Run OCR on *img*, find the first line matching TARGET_TEXT,
    and crop out its pixel region as a greyscale template.
    """
    scale = OCR_SCALE
    big   = img.resize((img.width * scale, img.height * scale), Image.LANCZOS).convert("L")
    arr   = np.array(big)

    data  = pytesseract.image_to_data(big, config=OCR_CONFIG,
                                      output_type=pytesseract.Output.DICT)
    lines = defaultdict(list)
    for i in range(len(data["text"])):
        w = data["text"][i].strip()
        if not w: continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines[key].append({
            "text": w,
            "left": data["left"][i],  "top": data["top"][i],
            "width": data["width"][i], "height": data["height"][i],
        })

    target_lower = TARGET_TEXT.lower()
    for words in lines.values():
        line_text = " ".join(w["text"] for w in words).lower()
        if target_lower in line_text:
            pad = 3
            x1  = max(0,            min(w["left"] for w in words) - pad)
            y1  = max(0,            min(w["top"]  for w in words) - pad)
            x2  = min(arr.shape[1], max(w["left"] + w["width"]  for w in words) + pad)
            y2  = min(arr.shape[0], max(w["top"]  + w["height"] for w in words) + pad)
            crop = arr[y1:y2, x1:x2]
            if crop.size > 0:
                return crop
    return None


def _classify_sign(img: Image.Image, bbox_scaled: tuple, verbose: bool = False) -> str:
    """
    Given the matched label's bbox (bx, by, bw, bh) in OCR_SCALE-upscaled pixel
    coordinates, sample the region directly below it — where the delta number
    renders — and classify it as a gain or a loss by brightness alone.

    A "+" delta renders in a bright, near-white colour; a "-" delta renders in a
    dim grey/teal that never reaches that brightness. Confirmed on real samples:
    a "+538,683" popup scored a 6.25% bright-pixel fraction in that band, a
    "-2,453,883" popup scored 0% — a wide, reliable margin, so no OCR is needed
    to tell them apart.
    """
    bx, by, bw, bh = bbox_scaled
    big = img.resize((img.width * OCR_SCALE, img.height * OCR_SCALE), Image.LANCZOS).convert("RGB")
    arr = np.array(big)

    y1 = max(0, by + bh)
    y2 = min(arr.shape[0], y1 + bh * 3)         # a few label-heights of room for the number
    x1 = max(0, bx - bw // 4)
    x2 = min(arr.shape[1], bx + bw + bw // 4)
    band = arr[y1:y2, x1:x2]

    if band.size == 0:
        # Nothing to sample — usually means the watch box is too short/mispositioned
        # to include the number line below the matched label at all.
        if verbose:
            print(f"  [sign] EMPTY sample band — label bbox=({bx},{by},{bw},{bh}) but the "
                  f"captured frame is only {arr.shape[1]}x{arr.shape[0]}px, leaving no room "
                  f"below the label. The watch box is likely too short to fit the number line.",
                  flush=True)
        return "-"

    brightness  = band.reshape(-1, 3).mean(axis=1)
    bright_frac = float((brightness > BRIGHT_LEVEL).mean())
    sign = "+" if bright_frac >= BRIGHT_FRACTION else "-"
    if verbose:
        print(f"  [sign] sample band={band.shape[1]}x{band.shape[0]}px  "
              f"bright_frac={bright_frac:.4f} (need >={BRIGHT_FRACTION})  -> {sign}", flush=True)
    return sign

# ── Core detection ─────────────────────────────────────────────────────────────

def _preprocess(img: Image.Image) -> np.ndarray:
    """Upscale x2 + greyscale — matches what the template was built from."""
    big = img.resize((img.width * OCR_SCALE, img.height * OCR_SCALE), Image.LANCZOS)
    return np.array(big.convert("L"))


def _count_via_template(scene: np.ndarray, template) -> int:
    """template can be a single array or a list — uses max score across all."""
    """
    Count stacked matches within a single vertical column.

    Algorithm:
      1. Find all (y, x) positions above MATCH_THRESHOLD using NMS
         (non-maximum suppression to get one peak per real match).
      2. Group peaks by X column — two peaks are in the same column if
         their X values are within template_width * X_MERGE_FACTOR of each other.
      3. For each column-group, count how many peaks are stacked vertically
         within MAX_LINE_SPACING_FACTOR * template_height of each other.
      4. Return REQUIRED_COUNT if any column-group has enough stacked lines.

    This correctly rejects the case where e.g. 2 separate windows each have
    1–2 lines — they land in different X columns and are never merged.
    """

    tlist = template if isinstance(template, list) else [template]
    tlist = [t for t in tlist if t.shape[0] <= scene.shape[0] and t.shape[1] <= scene.shape[1]]
    if not tlist:
        return 0, None
    # Use the first template's dimensions for NMS/clustering (all should be same size)
    th, tw = tlist[0].shape[:2]
    # Compute element-wise max response across all templates
    res = cv2.matchTemplate(scene, tlist[0], cv2.TM_CCOEFF_NORMED)
    for t in tlist[1:]:
        r2 = cv2.matchTemplate(scene, t, cv2.TM_CCOEFF_NORMED)
        h, w = min(res.shape[0], r2.shape[0]), min(res.shape[1], r2.shape[1])
        res[:h, :w] = np.maximum(res[:h, :w], r2[:h, :w])

    # ── NMS: proper 2D suppression within one template-box radius ─────────
    raw_ys, raw_xs = np.where(res >= MATCH_THRESHOLD)
    if len(raw_ys) == 0:
        return 0, None

    scores  = res[raw_ys, raw_xs]
    order   = np.argsort(scores)[::-1]
    all_pts = [(int(raw_ys[i]), int(raw_xs[i])) for i in order]
    peaks       = []
    suppressed  = set()
    for i, (y, x) in enumerate(all_pts):
        if i in suppressed:
            continue
        peaks.append((y, x))
        for j, (y2, x2) in enumerate(all_pts):
            if j not in suppressed and abs(y2 - y) < th and abs(x2 - x) < tw:
                suppressed.add(j)

    if len(peaks) < REQUIRED_COUNT:
        return 0, None

    # ── Group peaks by X column ────────────────────────────────────────────
    x_merge = tw * 0.5          # peaks within 50% of template width = same column
    peaks.sort(key=lambda p: p[1])   # sort by x
    columns = []   # list of lists of (y, x)
    for peak in peaks:
        merged = False
        for col in columns:
            if abs(peak[1] - col[0][1]) <= x_merge:
                col.append(peak)
                merged = True
                break
        if not merged:
            columns.append([peak])

    # ── Within each column, check for REQUIRED_COUNT vertically stacked ───
    max_gap = th * MAX_LINE_SPACING_FACTOR   # max gap between consecutive lines
    for col in columns:
        ys = sorted(p[0] for p in col)
        xs = [p[1] for p in col]
        # Sliding window of REQUIRED_COUNT consecutive y-values
        for i in range(len(ys) - REQUIRED_COUNT + 1):
            window = ys[i : i + REQUIRED_COUNT]
            # All consecutive gaps must be within max_gap
            if all(window[j+1] - window[j] <= max_gap for j in range(len(window)-1)):
                # Return count + bounding box of the cluster
                bx = min(xs)
                by = window[0]
                bw = tw
                bh = window[-1] - window[0] + th
                return REQUIRED_COUNT, (bx, by, bw, bh)

    return 0, None


def check_once(region=None, verbose=False):
    global _templates

    img = pyautogui.screenshot(region=region)

    # ── OCR-delta profiles: no template, the value differs every time ──────
    scene = _preprocess(img)

    # ── Warm path: template matching ──────────────────────────────────────
    if _templates:
        t0                 = time.perf_counter()
        count, bbox_scaled = _count_via_template(scene, _templates)
        ms                 = (time.perf_counter() - t0) * 1000
        found              = count >= REQUIRED_COUNT

        sign = ""
        if found and CLASSIFY_SIGN and bbox_scaled is not None:
            sign  = _classify_sign(img, bbox_scaled, verbose=verbose)
            found = (sign == "+")   # only a gain counts as "found" -> beeps; a loss is a no-op

        if CLASSIFY_SIGN:
            status = "✅ DETECTED (+)" if found else ("⏭️  ignored (-)" if sign else "❌ not found")
            print(f"[{time.strftime('%H:%M:%S')}] {status}  template={ms:.1f}ms", flush=True)
        else:
            status = "✅ DETECTED" if found else "❌ not found"
            print(f"[{time.strftime('%H:%M:%S')}] {status}  "
                  f"matched={count}/{REQUIRED_COUNT}  "
                  f"template={ms:.1f}ms", flush=True)

        # Convert bbox to screen-space using region offset, width = full scene width
        bbox_out = bbox_scaled
        if bbox_scaled is not None and region is not None:
            rx, ry = region[0], region[1]
            bbox_out = (rx + bbox_scaled[0], ry + bbox_scaled[1], scene.shape[1], bbox_scaled[3])
        return found, bbox_out, (sign if CLASSIFY_SIGN else count)

    # ── Cold path: OCR to build the template ──────────────────────────────
    print(f"[{time.strftime('%H:%M:%S')}] No template — running OCR to build one…", flush=True)
    t0      = time.perf_counter()
    tmpl    = _extract_template_from_ocr(img)
    ocr_ms  = (time.perf_counter() - t0) * 1000

    if tmpl is not None:
        _templates = [tmpl]
        _save_template(tmpl)
        count, bbox_scaled = _count_via_template(scene, _templates)
        found = count >= REQUIRED_COUNT

        sign = ""
        if found and CLASSIFY_SIGN and bbox_scaled is not None:
            sign  = _classify_sign(img, bbox_scaled, verbose=verbose)
            found = (sign == "+")

        status = ("✅ DETECTED (+)" if found else ("⏭️  ignored (-)" if sign else "❌ not found")) \
                 if CLASSIFY_SIGN else ("✅ DETECTED" if found else "❌ not found")
        print(f"[{time.strftime('%H:%M:%S')}] Template built! {status}  "
              f"matched={count}/{REQUIRED_COUNT}  ocr={ocr_ms:.0f}ms", flush=True)
        return found, bbox_scaled, (sign if CLASSIFY_SIGN else count)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] OCR ran ({ocr_ms:.0f}ms) but target text not visible — "
              f"waiting for '{TARGET_TEXT}' to appear so template can be built…", flush=True)
        return False, None, ("" if CLASSIFY_SIGN else 0)


def watch(region=None, interval=POLL_INTERVAL, verbose=False, overlay=None,
          enter_spam=True, enter_interval=0.005, click_interval=0.01):
    global _templates

    # Try to load a previously saved template so we start in warm mode
    loaded = _load_templates()
    if loaded:
        _templates = loaded
        print(f"[template] {len(loaded)} template(s) loaded — starting in fast mode", flush=True)
    else:
        print(f"[template] None found — will build on first OCR hit", flush=True)
    print(f"Watching for '{TARGET_TEXT}' ×{REQUIRED_COUNT}  "
          f"(region={region}) — Ctrl+C to stop\n", flush=True)

    global _paused, _enter_on
    _paused   = False
    _enter_on = False   # starts OFF — press ENTER_HOTKEY to start spamming
    keyboard.add_hotkey(HOTKEY, _toggle_pause)
    print(f"[hotkey] Press {HOTKEY.upper()} anytime (even while the game has focus) "
          f"to pause/resume detection.\n", flush=True)
    if enter_spam:
        keyboard.add_hotkey(ENTER_HOTKEY, _toggle_enter_spam)
        print(f"[hotkey] Press {ENTER_HOTKEY.upper()} to start/stop Enter+Left-Click spam "
              f"(starts OFF — Enter every {enter_interval*1000:.0f}ms, click every {click_interval*1000:.0f}ms "
              f"while nothing is detected, stops as soon as a hit is confirmed).\n", flush=True)

    last_state      = None
    frame_count     = 0
    t_start         = time.perf_counter()
    # A "+"/"-" popup is transient — it can be gone within one poll cycle — so
    # fire on the very first hit rather than waiting for repeated confirmation.
    DEBOUNCE_FRAMES = 1 if CLASSIFY_SIGN else 2
    BEEP_COOLDOWN   = 1.5 # seconds to wait before beeping again
    last_beep_time  = 0.0

    consec_found    = 0     # how many frames in a row we've seen a match
    last_enter_time = 0.0
    last_click_time = 0.0
    spamming        = None  # tracks transitions for the console log only

    while True:
        try:
            t0 = time.perf_counter()

            if _paused:
                if overlay:
                    overlay.set_paused(True)
                    overlay.set_hit(False)
                    overlay.set_text(f"PAUSED  ({HOTKEY.upper()} to resume)")
                time.sleep(0.1)   # idle poll — no screenshots, no Enter, no detection
                continue
            elif overlay:
                overlay.set_paused(False)

            # Track the overlay's live region so dragging/resizing the box
            # moves what's actually being watched, not just its outline.
            cur_region = tuple(overlay.region) if overlay else region

            found, bbox, extra = check_once(cur_region, verbose=verbose)
            frame_count += 1
            elapsed      = t0 - t_start
            fps          = frame_count / elapsed if elapsed > 0 else 0

            if found:
                consec_found += 1
            else:
                consec_found = 0

            # Debounce: only act on found after DEBOUNCE_FRAMES consecutive hits
            stable_found = found and consec_found >= DEBOUNCE_FRAMES

            # Spam Enter + left-click while nothing is detected, but only when the
            # ENTER_HOTKEY toggle is on; stop the instant a hit is detected either way.
            enter_active = enter_spam and _enter_on
            if enter_active:
                if not stable_found:
                    # pydirectinput is built specifically for games that read
                    # DirectInput device state rather than the Windows message
                    # queue or global keyboard hooks — both pyautogui's VK press
                    # and keyboard's scan-code SendInput can be invisible to
                    # those games, since neither touches the DirectInput buffer
                    # pydirectinput targets. Enter and click run on independent
                    # cadences (different intervals), not tied to each other.
                    if t0 - last_enter_time >= enter_interval:
                        pydirectinput.press("enter")
                        last_enter_time = t0
                    if t0 - last_click_time >= click_interval:
                        pydirectinput.click()   # left mouse click at the current cursor position
                        last_click_time = t0
                if stable_found != spamming:
                    print(f"  [enter+click] {'paused — hit detected' if stable_found else 'resumed'}", flush=True)
                    spamming = stable_found
            elif spamming is not None:
                spamming = None   # reset so the next ON period logs cleanly

            if stable_found != last_state:
                if stable_found:
                    if CLASSIFY_SIGN:
                        print(f"  *** ALERT: {TARGET_TEXT} +  ***  fps={fps:.1f}\n")
                    else:
                        print(f"  *** ALERT: text appeared on {REQUIRED_COUNT} separate lines! ***  fps={fps:.1f}\n")
                    if enter_active:
                        # A hit turns the box red and locks Enter+Click off for good —
                        # it will NOT resume on its own once the condition clears;
                        # only a fresh ENTER_HOTKEY press turns it back on.
                        _enter_on = False
                        print(f"  [enter+click] OFF — hit detected. Press {ENTER_HOTKEY.upper()} to re-enable.\n", flush=True)
                    now = time.perf_counter()
                    if now - last_beep_time >= BEEP_COOLDOWN:
                        threading.Thread(target=beep, daemon=True).start()
                        last_beep_time = now
                else:
                    print(f"  (condition cleared)  fps={fps:.1f}\n")
                last_state = stable_found

            enter_tag = ""
            if enter_spam:
                enter_tag = f"  [Enter+Click: {'ON' if _enter_on else 'OFF'}]"

            if overlay:
                overlay.set_hit(stable_found)
                # Fast, per-frame status text — no OCR, so no risk of garbage
                # noise filling the box; just reflects what this check found.
                if CLASSIFY_SIGN:
                    if stable_found:
                        overlay.set_text(f"{TARGET_TEXT}  +  (beep!){enter_tag}")
                    elif extra == "-":
                        overlay.set_text(f"{TARGET_TEXT}  -  (ignored){enter_tag}")
                    else:
                        overlay.set_text(f"watching…{enter_tag}")
                else:
                    overlay.set_text(f"{TARGET_TEXT}  {extra}/{REQUIRED_COUNT}{enter_tag}")

            if interval > 0:
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            break

# ── Region persistence ────────────────────────────────────────────────────────

def save_region(region: tuple):
    import json
    with open(REGION_FILE, "w") as f:
        json.dump({"region": list(region)}, f)


def load_region() -> tuple | None:
    import json
    if REGION_FILE.exists():
        try:
            data = json.loads(REGION_FILE.read_text())
            r = tuple(data["region"])
            print(f"[region] Loaded saved region {r}", flush=True)
            return r
        except Exception:
            pass
    return None

# ── Persistent overlay rectangle ──────────────────────────────────────────────

# ── Persistent overlay rectangle ──────────────────────────────────────────────

class OverlayApp:
    """
    Runs entirely on the MAIN thread via root.after() polling.
    The watch loop runs in a background thread and communicates via
    thread-safe attributes on this object.

    The box border and corner handles are drag/resize handles: the interior is
    rendered in the OS colour-key (black == -transparentcolor), which Windows
    also makes click-through, so only the border stroke and handle squares
    (opaque, non-key-colour pixels) can ever receive mouse events.
    """
    BORDER      = 3
    COLOR        = "#00FF88"
    COLOR_HIT    = "#FF4444"
    COLOR_PAUSED = "#999999"
    HANDLE_SIZE  = 8
    MIN_SIZE     = 20

    def __init__(self, region: tuple, watch_fn):
        self.region      = list(region)   # [x, y, w, h] — mutable, moved/resized live
        self.watch_fn    = watch_fn   # callable, blocks until Ctrl+C
        self.hit         = False
        self.paused      = False
        self.status_text = "watching…"
        self.root        = None
        self._canvas     = None
        self._rect_id    = None
        self._text_id    = None
        self._handle_ids = {}
        self._drag       = {}

    def set_hit(self, hit: bool):
        """Called from watch thread — just set a flag; UI update happens in after() tick."""
        self.hit = hit

    def set_paused(self, paused: bool):
        """Called from watch thread — flips the border to a distinct grey while paused."""
        self.paused = paused

    def set_text(self, text: str):
        """Called from watch thread — replace the box's caught-text; UI syncs in after() tick."""
        self.status_text = text or "watching…"

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
                                                 width=b*2, fill="black", tags="border")
        self._text_id = canvas.create_text(0, 0, text=self.status_text, anchor="nw",
                                            fill=self.COLOR, font=("Segoe UI", 10, "bold"),
                                            width=400, tags="status")
        for corner in ("nw", "ne", "sw", "se"):
            hid = canvas.create_rectangle(0, 0, 0, 0, fill=self.COLOR, outline="",
                                           tags=("handle", f"handle_{corner}"))
            self._handle_ids[corner] = hid

        canvas.tag_bind("border", "<ButtonPress-1>",   self._on_move_press)
        canvas.tag_bind("border", "<B1-Motion>",       self._on_move_drag)
        canvas.tag_bind("border", "<ButtonRelease-1>", self._on_release)
        for corner in ("nw", "ne", "sw", "se"):
            canvas.tag_bind(f"handle_{corner}", "<ButtonPress-1>",
                             lambda e, c=corner: self._on_resize_press(e, c))
            canvas.tag_bind(f"handle_{corner}", "<B1-Motion>",       self._on_resize_drag)
            canvas.tag_bind(f"handle_{corner}", "<ButtonRelease-1>", self._on_release)

        self._canvas = canvas
        self.root    = root
        self._sync_geometry()
        self._exclude_from_capture()
        return root

    def _exclude_from_capture(self):
        """
        Hide this overlay window from screen-capture APIs (pyautogui's
        screenshot included) while it stays fully visible on the physical
        display. Without this, the box's own border/handles/status text get
        captured as part of the very screenshot used for detection, silently
        corrupting template matching whenever they overlap the watched
        content — this is what was causing "+" popups to go undetected: the
        "watching…" status text was rendering directly on top of the real
        "Combat Power Change" label in every screenshot.
        """
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
                print("  [warning] Could not exclude overlay from screen capture "
                      "(unsupported OS build?) — the box's own text may interfere with detection.",
                      flush=True)
        except Exception as e:
            print(f"  [warning] Could not exclude overlay from screen capture: {e}", flush=True)

    def _sync_geometry(self):
        """Resize the window + canvas to match self.region, then redraw border/handles/text."""
        x, y, w, h = self.region
        b, hs = self.BORDER, self.HANDLE_SIZE
        W, H = w + b*2, h + b*2
        self.root.geometry(f"{W}x{H}+{x - b}+{y - b}")
        self._canvas.config(width=W, height=H)
        self._canvas.coords(self._rect_id, 0, 0, W - 1, H - 1)
        self._canvas.coords(self._text_id, b + 4, b + 4)
        for corner, (hx, hy) in {"nw": (0, 0), "ne": (W, 0), "sw": (0, H), "se": (W, H)}.items():
            self._canvas.coords(self._handle_ids[corner], hx-hs, hy-hs, hx+hs, hy+hs)

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

    def _poll(self, root):
        """Called every 50 ms on the main thread to sync hit state → border colour + caught text."""
        if self._canvas:
            color = self.COLOR_PAUSED if self.paused else (self.COLOR_HIT if self.hit else self.COLOR)
            self._canvas.itemconfig("border", outline=color)
            self._canvas.itemconfig("status", text=self.status_text, fill=color)
            for hid in self._handle_ids.values():
                self._canvas.itemconfig(hid, fill=color)
        root.after(50, self._poll, root)

    def run(self):
        """Start overlay window + launch watch loop in background thread, then enter mainloop."""
        root = self._build_window()
        # Start watch in a daemon thread
        t = threading.Thread(
            target=self.watch_fn,
            kwargs={"overlay": self},
            daemon=True
        )
        t.start()
        root.after(50, self._poll, root)
        root.mainloop()


# ── Visual region selector ─────────────────────────────────────────────────────

HANDLE_SIZE  = 10
BORDER_COLOR = "#00FF88"
HANDLE_COLOR = "#FFFFFF"
DIM_COLOR    = "#000000"

class RegionSelector:
    MIN_SIZE = 20

    def __init__(self, initial_region=None):
        self.result = None   # set on confirm

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
            self.rx1, self.ry1 = cx - 200, cy - 100
            self.rx2, self.ry2 = cx + 200, cy + 100

        self._drag_data = {}
        self._info_font = tkfont.Font(family="Courier", size=11, weight="bold")
        self._bind_events()
        self._draw()

    def _draw(self):
        c  = self.canvas
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
            c.create_rectangle(hx-HANDLE_SIZE, hy-HANDLE_SIZE,
                                hx+HANDLE_SIZE, hy+HANDLE_SIZE,
                                fill=HANDLE_COLOR, outline=BORDER_COLOR, width=1)
        w, h = x2-x1, y2-y1
        label = f"  {w}x{h}px  |  ({x1},{y1})  |  Enter=Confirm  Esc=Quit  "
        lx = x1 + w//2
        ly = y1 - 18 if y1 > 30 else y2 + 18
        c.create_text(lx+1, ly+1, text=label, font=self._info_font, fill="black",  anchor="center")
        c.create_text(lx,   ly,   text=label, font=self._info_font, fill=BORDER_COLOR, anchor="center")

    def _handle_positions(self):
        x1, y1, x2, y2 = self.rx1, self.ry1, self.rx2, self.ry2
        mx, my = (x1+x2)//2, (y1+y2)//2
        return [(x1,y1,"h_nw"),(mx,y1,"h_n"),(x2,y1,"h_ne"),
                (x1,my,"h_w"),               (x2,my,"h_e"),
                (x1,y2,"h_sw"),(mx,y2,"h_s"),(x2,y2,"h_se")]

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
            if hx-HANDLE_SIZE <= ex <= hx+HANDLE_SIZE and hy-HANDLE_SIZE <= ey <= hy+HANDLE_SIZE:
                return tag
        return None

    def _on_press(self, event):
        ex, ey = event.x, event.y
        handle = self._hit_handle(ex, ey)
        if handle:
            self._drag_data = {"mode": "resize", "handle": handle}
        elif self.rx1 <= ex <= self.rx2 and self.ry1 <= ey <= self.ry2:
            self._drag_data = {"mode": "move", "ox": ex-self.rx1, "oy": ey-self.ry1}
        else:
            self._drag_data = {}

    def _on_drag(self, event):
        ex, ey = event.x, event.y
        d = self._drag_data
        if not d: return
        if d["mode"] == "move":
            w, h = self.rx2-self.rx1, self.ry2-self.ry1
            self.rx1, self.ry1 = ex-d["ox"], ey-d["oy"]
            self.rx2, self.ry2 = self.rx1+w, self.ry1+h
        elif d["mode"] == "resize":
            tag = d["handle"]
            if "w" in tag: self.rx1 = min(ex, self.rx2-self.MIN_SIZE)
            if "e" in tag: self.rx2 = max(ex, self.rx1+self.MIN_SIZE)
            if "n" in tag: self.ry1 = min(ey, self.ry2-self.MIN_SIZE)
            if "s" in tag: self.ry2 = max(ey, self.ry1+self.MIN_SIZE)
        self._draw()

    def _on_release(self, _): self._drag_data = {}

    def _confirm(self):
        x1, y1 = min(self.rx1,self.rx2), min(self.ry1,self.ry2)
        x2, y2 = max(self.rx1,self.rx2), max(self.ry1,self.ry2)
        self.result = (x1, y1, x2-x1, y2-y1)
        self.root.destroy()

    def _quit(self):
        self.root.destroy()
        print("Cancelled.")
        sys.exit(0)

    def run(self) -> tuple:
        """Block until confirmed. Returns (left, top, w, h)."""
        self.root.mainloop()
        return self.result


# ── Entry point ────────────────────────────────────────────────────────────────

def _apply_profile(profile: dict):
    global TARGET_TEXT, REQUIRED_COUNT, TEMPLATE_FILE, MATCH_THRESHOLD, CLASSIFY_SIGN
    TARGET_TEXT     = profile["target"]
    REQUIRED_COUNT  = profile["count"]
    tmpl            = profile["template"]
    base            = Path(__file__).parent
    TEMPLATE_FILE   = [base / t for t in tmpl] if isinstance(tmpl, list) else [base / tmpl]
    MATCH_THRESHOLD = profile.get("threshold", MATCH_THRESHOLD)
    CLASSIFY_SIGN   = profile.get("classify_sign", False)


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
    parser = argparse.ArgumentParser(description="Screen text detector.")
    parser.add_argument("--region", nargs=4, type=int, metavar=("L","T","W","H"))
    parser.add_argument("--once",     action="store_true")
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL)
    parser.add_argument("--debug",    action="store_true")
    parser.add_argument("--reset-template", action="store_true")
    parser.add_argument("--reselect",  action="store_true", help="re-open the region selector even if a region is saved")
    parser.add_argument("--profile", choices=list(PROFILES.keys()))
    parser.add_argument("--no-enter-spam", action="store_true", help="disable the Enter+Left-Click spam feature entirely (no hotkey, no presses/clicks)")
    parser.add_argument("--enter-interval", type=float, default=0.005, help="seconds between spammed Enter presses (default: 0.005 = 5ms)")
    parser.add_argument("--click-interval", type=float, default=0.01, help="seconds between spammed left-clicks (default: 0.01 = 10ms)")
    parser.add_argument("--hotkey", default=HOTKEY, help="global pause/resume hotkey (default: f6)")
    parser.add_argument("--enter-hotkey", default=ENTER_HOTKEY, help="global Enter-spam on/off hotkey (default: f9)")
    parser.add_argument("--elevate", action="store_true",
                         help="re-launch this script as Administrator (fixes hotkeys not responding "
                              "while an elevated game window has focus)")
    args = parser.parse_args()
    HOTKEY       = args.hotkey
    ENTER_HOTKEY = args.enter_hotkey

    if args.elevate and not _is_admin():
        print("  Re-launching elevated (UAC prompt incoming)...")
        _relaunch_as_admin()

    if platform.system() == "Windows" and not _is_admin():
        print("  [warning] Not running as Administrator. If the game runs elevated, "
              f"{HOTKEY.upper()}/{ENTER_HOTKEY.upper()} may stop responding while it has focus — "
              "re-run with --elevate (or as Administrator) if that happens.\n")

    # 1. Profile selection — no prompt, defaults to DEFAULT_PROFILE unless --profile is given
    profile = PROFILES[args.profile] if args.profile else PROFILES[DEFAULT_PROFILE]
    _apply_profile(profile)
    print(f"  Profile: {profile['name']}  x{REQUIRED_COUNT} stacked lines")

    # 2. Reset template if requested
    if args.reset_template:
        deleted = False
        for f in TEMPLATE_FILE:
            if Path(f).exists():
                Path(f).unlink()
                print(f"  Template deleted: {f}")
                deleted = True
        if not deleted:
            print("  No saved template found.")
        sys.exit(0)

    # 3. Region selection — reuse the saved region with no prompt; only open the
    # selector when there isn't one yet (or --region/--reselect asks for it).
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

    print(f"\n  Starting... region={region}\n")

    # 4. Build watch function bound to region+args
    def _watch_fn(overlay=None):
        watch(region, args.interval, verbose=args.debug, overlay=overlay,
              enter_spam=not args.no_enter_spam, enter_interval=args.enter_interval,
              click_interval=args.click_interval)

    # 5. Launch — OverlayApp owns the main thread (tkinter requirement)
    if args.once:
        loaded = _load_templates()
        if loaded: _templates = loaded
        found, _bbox, _count = check_once(region, verbose=args.debug)
        sys.exit(0 if found else 1)
    else:
        app = OverlayApp(region, _watch_fn)
        app.run()