#!/usr/bin/env python3
"""
Draggable/resizable green box overlay that OCRs the "+<number>" delta inside
it — e.g. "+538,683" from a "Combat Power Change" popup (see +delata_ocr.png:
bold white text with a black outline on a dark green/olive panel).

Position the box over the number, and it's continuously screenshotted,
preprocessed (upscaled + grayscaled), and OCR'd with Tesseract. Whenever a
"+<digits>" match appears, the control window shows it in its status and
recent activity list; a beep fires once per new value (not every frame the
popup happens to still be on screen). The OCR loop runs on its own background
thread, back-to-back
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

Press F7 to auto-locate the "Combat Power Change" panel: screenshots the
desktop, finds the panel via template matching against
assets/reference/combat_power_label.png, snaps the box onto the number field
beneath it, and moves the cursor onto the "Reset x1" button — no manual
dragging or aiming needed. For now this only recognizes that one specific
reset dialog (the template is a crop of its exact label) — it won't find
any other popup/reset screen.

Requirements:
    pip install pillow pytesseract mss keyboard pydirectinput opencv-python numpy
Tesseract OCR engine:
    - Windows : https://github.com/UB-Mannheim/tesseract/wiki
    - macOS   : brew install tesseract
    - Linux   : sudo apt install tesseract-ocr
"""

# PyInstaller starts spawned capture helpers through this entry point. This must
# run before normal imports so a helper never builds the Tk application.
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

import re
import sys
import time
import json
import math
import random
import queue
import platform
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from pathlib import Path

# ── DPI awareness (Windows) ──────────────────────────────────────────────────
# Must be set before any window/screen API runs (Tk included), so every
# screen-coordinate value in this app agrees on physical pixels: mss
# screenshots, pydirectinput's cursor moves (which read GetSystemMetrics),
# and Tk's own window geometry. A DPI-unaware process gets a scaled/
# virtualized view of the screen from Windows for GetSystemMetrics, while mss
# still captures true physical pixels regardless — so on any display running
# above 100% scaling, a screenshot-based coordinate and a cursor move to that
# same coordinate silently disagree, showing up as the cursor landing
# consistently off to one side of wherever auto-locate aims it.
if platform.system() == "Windows":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from recorded_alert import RecordedAlert
from discord_alerts import DiscordNotifier, bind_foreground_target, target_is_current

try:
    import mss              # screen capture — ~2x faster and far more consistent than pyautogui.screenshot
    import pytesseract
    import cv2              # template matching for F7 auto-locate
    import numpy as np
    from PIL import Image, ImageDraw, ImageOps, ImageTk
    import keyboard        # global hotkey to start/stop Enter+click spam — works even while the game has focus
    import pydirectinput   # DirectInput-style key injection — see the spam-loop comment below
except ImportError as e:
    tk.Tk().withdraw()
    messagebox.showerror("Maple Tapper Looker", f"Missing dependency: {e}\n\nRun:\npip install -r requirements.txt")
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
UI_BG = "#09070D"
UI_SURFACE = "#17111F"
UI_BORDER = "#654575"
UI_BUTTON = "#654384"
UI_TEXT = "#FAF4FF"
UI_MUTED = "#CBBBD8"
UI_PRIMARY = "#F078CF"
UI_PRIMARY_ACTIVE = "#FFA6E8"
UI_ACCENT = "#A997FF"

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
# The delta popup renders its digits in a light, low-saturation blue-gray over a darker, far more
# saturated teal/olive panel — measured (168,190,198) directly off two different real screenshots of
# it. Converting straight to grayscale collapses much of that contrast away (digit and background can
# land at similar brightness once hue/saturation are discarded), which was leaving OCR reading
# digits close to 0% confidence and misreading them outright on some real captures. Masking on
# saturation/value in preprocess_for_ocr instead — bright AND desaturated pixels are digit ink,
# everything else is background — measured 90%+ confidence with exact correct reads on the same
# screenshots that plain grayscale failed on.
OCR_MASK_VALUE_MIN      = 140   # HSV V — digit pixels are brighter than the panel background
OCR_MASK_SATURATION_MAX = 70    # HSV S — digit pixels are far less saturated than the teal/olive panel
BEEP_COOLDOWN     = 1.5    # min seconds between beeps
CONFIRM_ATTEMPTS  = 5      # follow-up OCR reads tried after a raw detection before giving up on it as
                           # a false positive — spam is already stopped by then, so retrying costs
                           # nothing but a little time (see _ocr_loop for why this isn't just 1 try)

# Debug capture: whenever OCR sees text that looks like it might be a garbled number (digits present)
# but it doesn't parse as a valid "+<number>", save the raw crop here so real missed-detection cases
# can be collected from actual play and used to recalibrate OCR_MASK_*/etc. against real failures
# instead of guessing — this is the same "measure against a real screenshot" approach that fixed
# everything else OCR/auto-locate related so far this project. Rate-limited so a sustained streak of
# garbled reads doesn't flood the folder; saves both the raw crop and what the mask did to it, since
# both are useful for diagnosing which stage actually failed.
DEBUG_CAPTURE_DIR      = _BASE_DIR / "debug_captures"
DEBUG_CAPTURE_COOLDOWN = 3.0

# Startup re-check of the captures the PREVIOUS run left behind: collecting images
# during play only pays off if something actually reads them back, and doing that by
# hand never happens. Every launch re-runs the current detection pipeline over the
# captures it hasn't judged yet and records the verdict in CAPTURE_CHECK_LEDGER, so
# each classifier change gets scored against real frames automatically — hit_*.png
# files carry the confirmed value in their name, which makes them a labelled
# correct/wrong test, and a miss_*_raw.png that now reads as a "+<number>" is a
# failure the current code has recovered.
#
# The ledger is also what "already checked" means: filenames are left untouched on
# purpose, because tools/build_digit_templates.py globs hit_*.png and parses the
# value straight out of the name — marking by renaming would quietly break template
# rebuilds. It doubles as crash protection, since a run that dies halfway keeps the
# verdicts it already wrote and resumes from there rather than starting over.
# hit captures whose filename-encoded value is known NOT to match the image, from
# before read_delta was fixed to return the exact frame that produced a result (it
# used to re-grab a separate, later screenshot that could show a different number).
# Their labels can't be trusted, so they are neither training data nor a fair test:
# grading against them would report two permanent regressions that no classifier
# change can ever fix. Lives here rather than in tools/build_digit_templates.py —
# which is where it started, and still consumes it — because the startup re-check
# needs it too and tools/ is not bundled into the exe.
KNOWN_BAD_CAPTURES = {
    "hit_20260914_231259_+633221.png",
    "hit_20260914_233154_+17971.png",
}

CAPTURE_CHECK_LEDGER  = DEBUG_CAPTURE_DIR / "_checked.json"
CAPTURE_CHECK_CUTOFF  = "20260914"   # ignore captures stamped before this date (YYYYMMDD)
CAPTURE_CHECK_YIELD   = 0.004        # pause between images so the OCR loop keeps priority
CAPTURE_CHECK_FLUSH   = 200          # write the ledger every N images, not just at the end
HIT_CAPTURE_RE  = re.compile(r"^hit_(\d{8})_\d{6}_([+-]?\d+)\.png$")
MISS_CAPTURE_RE = re.compile(r"^miss_(\d{8})_\d{6}_raw\.png$")

ENTER_HOTKEY    = "f9"    # toggles Enter+Left-Click spam on/off — starts OFF
ENTER_INTERVAL  = 0.16    # seconds between spammed Enter presses (160ms)
CLICK_INTERVAL  = 0.24    # seconds between spammed left-clicks (240ms)
SPAM_JITTER     = 0.3     # +/- fraction of randomness applied to each interval above,
                          # so presses/clicks don't land on a perfectly robotic fixed cadence
BEEP_HOTKEY     = "f10"   # mutes/unmutes the detection beep — starts UNMUTED

AUTOLOCATE_HOTKEY = "f7"   # scans the screen for the Combat Power Change panel and snaps the box + cursor to it
LABEL_TEMPLATE_PATH = Path(__file__).parent / "assets" / "reference" / "combat_power_label.png"
AUTOLOCATE_MIN_CONFIDENCE = 0.75    # cv2.TM_CCOEFF_NORMED score below this is treated as "not found"
AUTOLOCATE_SCALES = np.linspace(0.5, 2.0, 31)   # search these template scales to handle different UI/DPI scaling
# Both the BEFORE and AFTER panels show a "Combat Power Change" label (BEFORE's value is always 0), so
# template matching finds two near-identical matches — the rightmost one is always the AFTER panel, which
# is the one that actually carries a value. These ratios (label size -> number field offset/size) were
# measured directly off assets/reference/combat_power_label.png vs the number field beneath it in the
# screenshot it was cropped from, expressed relative to the matched label's own size so they still hold
# at whatever scale the label was actually found at.
LABEL_TO_NUMBER_DX_RATIO = -11 / 146
LABEL_TO_NUMBER_DY_RATIO = 27 / 10
NUMBER_WIDTH_RATIO       = 154 / 146
NUMBER_HEIGHT_RATIO      = 22 / 10
# The number field's exact measured height above is a snug fit around the digits, which was clipping
# their tops/bottoms just enough to hurt OCR — pad it out top and bottom by this fraction of the label's
# own height on each side.
NUMBER_VERTICAL_PAD_RATIO = 0.6

# Same idea for the Reset button(s) beneath the panels — measured the same way (BEFORE label
# top-left to button top-left), so the cursor can be moved onto the actual button (center of this
# rect) instead of onto the number field. The button row is centered under the WHOLE dialog rather
# than fixed relative to the BEFORE panel, so its offset from BEFORE's label genuinely differs
# between a Reset x1 dialog (2 panels wide, measured from assets/reference/combat_power_label.png's
# own screenshot) and a Reset x3 dialog (4 panels wide, measured from a Reset x3 screenshot) — one
# set of ratios does not work for both, so _auto_locate() picks between them by detected layout.
LABEL_TO_RESET_X1_DX_RATIO = 68 / 146
LABEL_TO_RESET_X1_DY_RATIO = 131 / 10
RESET_X1_WIDTH_RATIO       = 247 / 146
RESET_X1_HEIGHT_RATIO      = 29 / 10

LABEL_TO_RESET_X3_DX_RATIO = 825 / 277
LABEL_TO_RESET_X3_DY_RATIO = 246 / 19
RESET_X3_WIDTH_RATIO       = 344 / 277
RESET_X3_HEIGHT_RATIO      = 55 / 19

AUTOLOCATE_MOVE_DURATION = 0.55   # seconds to glide the cursor to the Reset button, instead of teleporting
AUTOLOCATE_MOVE_STEPS    = 45     # interpolation steps across that duration — pydirectinput's own
                                  # duration/tween params are accepted but silently ignored (always an
                                  # instant jump), so the easing is done by hand here
AUTOLOCATE_MOVE_JITTER_PX = 4     # max sideways wobble off the straight-line path, shrinking to 0 as it
                                  # nears the target — reads as a human hand instead of a robotic slide

# psm 6 = "uniform block of text" — the box may contain the label line above
# the number, so don't assume a single line. Whitelist keeps OCR focused on
# the characters a delta number can actually contain. Plain grayscale+upscale
# (no binarization) turned out to OCR this font far more reliably than any
# brightness threshold — thresholding was breaking up the dashed/outlined
# strokes of the game's stylized digits.
OCR_CONFIG = r'--psm 6 -c tessedit_char_whitelist=+-0123456789,'

PLUS_NUMBER_RE = re.compile(r'\+\d[\d,]*\d')  # "+<number>" only, by design: a negative/bad reset
                                               # result should NOT count as detected, so the spam
                                               # keeps re-rolling through it — only a positive result
                                               # stops it. (A wide auto-locate box spanning multiple
                                               # AFTER panels still works fine with this: .search()
                                               # finds the first "+" among them, if any appear.)
                                               # Must END on a digit: Tesseract sometimes tacks a
                                               # stray comma onto the end ('+19,525,' seen on a real
                                               # capture) and the old '\+[\d,]{2,}' swallowed it
                                               # into the displayed/logged value.

# Template matching for the digits/sign, tried before ever falling back to Tesseract. Tesseract has
# a documented failure mode on this exact font (dropping the leading '+' — see _trim_to_text_band —
# and misreading digits under real gameplay conditions), while matching each character against known
# reference glyphs captured from actual CONFIRMED-correct detections scored 100% accuracy across every
# usable real example collected so far (54/54 images, 370/370 characters, leave-one-out cross-
# validated). It's also faster per read (~62ms vs Tesseract's ~135ms on the same crop). Templates are
# built offline from debug_captures/hit_*.png (see assets/digit_templates.npz) — this is deliberately
# not an online/self-updating system: new captures don't change live behavior until someone rebuilds
# and redeploys the template file, so a bad capture can't silently degrade detection on its own.
DIGIT_TEMPLATE_PATH   = Path(__file__).parent / "assets" / "digit_templates.npz"
TEMPLATE_GLYPH_SIZE   = (28, 40)   # (w, h) every glyph is resized to before comparison — must match
                                   # whatever size the templates in DIGIT_TEMPLATE_PATH were built at
TEMPLATE_MIN_WIDTH    = 6          # discard thinner blobs as noise (e.g. a stray panel-border line)
TEMPLATE_GAP_RATIO    = 2.5        # a glyph-to-glyph gap wider than this multiple of the median glyph
                                   # width marks a new number, not a new character — needed since a
                                   # wide auto-locate box can span multiple AFTER panels (Reset x3) at
                                   # once, each with its own number
TEMPLATE_MATCH_MIN_SCORE = 0.5     # cv2.TM_CCOEFF_NORMED score below this = not confident enough;
                                   # triggers the Tesseract fallback for that one read instead of
                                   # trusting a shaky template match
TEMPLATE_PLUS_MIN_SCORE  = 0.7     # threshold for _is_plus_sign specifically — measured directly:
                                   # real '+' glyphs score ~1.0, every digit and '-' sample score
                                   # <= 0.4, so this has a wide, confirmed margin on both sides
TEMPLATE_MIN_RUN_BOXES   = 2       # a run of a single blob can never be a "+<number>" (PLUS_NUMBER_RE
                                   # needs a sign plus at least two more characters), so it is junk to
                                   # skip rather than a number to puzzle over — see
                                   # classify_number_via_templates for why that distinction mattered:
                                   # the app photographs its own overlay border, whose bottom edge the
                                   # LANCZOS upscale in preprocess_for_ocr blends into a mask-passing
                                   # grey smudge at the right-hand edge of the frame. Measured on real
                                   # play data: that one smudge was forcing the Tesseract fallback on
                                   # 141 of 141 negative rolls in a single session (~128ms each,
                                   # against ~0.3ms for a clean template read).
TEMPLATE_SIGN_MAX_HEIGHT_FRAC = 0.5  # a glyph taller than this fraction of the text band is a DIGIT,
                                   # not a sign — measured across real captures with no overlap:
                                   # '-' spans 0.10 of the band, '+' 0.33, digits 0.72-0.87. Used by
                                   # _classify_number_run to tell "this number is negative" apart from
                                   # "this number's sign is missing from the crop", which look
                                   # identical to _is_plus_sign but must not be treated the same.


def _load_digit_templates():
    try:
        data = np.load(DIGIT_TEMPLATE_PATH)
        key_for = {"+": "plus", "-": "minus"}
        return {cls: data[key_for.get(cls, cls)] for cls in data["class_names"]}
    except Exception as e:
        print(f"  [warning] Digit templates unavailable, falling back to Tesseract only: {e}", flush=True)
        return None


DIGIT_TEMPLATES = _load_digit_templates()


def _pack_templates(templates):
    """Flatten every stored reference glyph into one row of a single matrix,
    zero-meaned and unit-normalised, so that matching a glyph against ALL
    370 of them is one matrix-vector product instead of 370 separate
    cv2.matchTemplate calls. This is exactly the same number, not an
    approximation: TM_CCOEFF_NORMED between two images of equal size IS the
    dot product of their zero-mean, unit-norm flattenings, and the scores
    agree with cv2's to within 3e-6 across every real glyph checked.
    Measured per glyph: 9.3ms -> 0.07ms for digit classification, 1.6ms ->
    0.06ms for the '+' check — and the '+' check runs on every frame that
    has any glyphs in it at all, so this is a ~50ms cut off the one read
    that matters most, the frame where the popup first appears."""
    classes, rows = [], []
    for cls, samples in templates.items():
        for sample in samples:
            v = sample.astype("float32").ravel()
            v -= v.mean()
            norm = np.linalg.norm(v)
            rows.append(v / norm if norm else v)
            classes.append(str(cls))
    classes = np.array(classes)
    return {
        "classes": classes,
        "matrix": np.ascontiguousarray(np.stack(rows)),
        "is_plus": classes == "+",
        "is_digit": ~np.isin(classes, ("+", "-")),
    }


_TEMPLATE_PACK = _pack_templates(DIGIT_TEMPLATES) if DIGIT_TEMPLATES else None


def _template_scores(glyph_arr):
    """TM_CCOEFF_NORMED of *glyph_arr* against every packed template at
    once — see _pack_templates. A flat (zero-variance) glyph scores 0
    everywhere, matching cv2's behaviour for the same input."""
    v = _resize_for_template(glyph_arr).astype("float32").ravel()
    v -= v.mean()
    norm = np.linalg.norm(v)
    if norm:
        v /= norm
    return _TEMPLATE_PACK["matrix"] @ v


def _segment_glyph_boxes(mask_img: Image.Image):
    """Column-gap segmentation: return (x0, x1, y0, y1) boxes for each ink
    blob in *mask_img*, left to right, dropping anything too thin to be a
    real character (see TEMPLATE_MIN_WIDTH)."""
    arr = np.array(mask_img)
    ink_cols = (arr < 128).any(axis=0)
    spans = []
    start = None
    for x, has_ink in enumerate(ink_cols):
        if has_ink and start is None:
            start = x
        elif not has_ink and start is not None:
            spans.append((start, x))
            start = None
    if start is not None:
        spans.append((start, len(ink_cols)))

    boxes = []
    for x0, x1 in spans:
        if x1 - x0 < TEMPLATE_MIN_WIDTH:
            continue
        ink_rows = np.where((arr[:, x0:x1] < 128).any(axis=1))[0]
        if len(ink_rows) == 0:
            continue
        boxes.append((x0, x1, int(ink_rows.min()), int(ink_rows.max()) + 1))
    return boxes


def _is_comma_box(box, img_height) -> bool:
    """A comma sits low and short; digits/signs span much more of the row
    height. Commas are recognized this way, by position, rather than
    template-matched — reinserted programmatically from digit count instead
    (see _classify_number_run) — since it's a small, easily-confused glyph
    that isn't actually needed to read the number's value."""
    _x0, _x1, y0, y1 = box
    return (y1 - y0) < img_height * 0.35 and y1 / img_height > 0.7


def _resize_for_template(glyph_arr, size=TEMPLATE_GLYPH_SIZE):
    """cv2.INTER_AREA is for shrinking and gives corrupted results when used
    to enlarge a small image — confirmed directly: a minus sign's natural
    ~4px-tall crop, stretched to the 40px template height with INTER_AREA,
    produced a solid corrupted blob instead of a stretched bar, which then
    wrongly out-scored real digits during classification. Digits are close
    enough to the target size that this mostly didn't show up for them, but
    it must still be handled correctly in general — use INTER_AREA only
    when actually shrinking, INTER_LINEAR when enlarging."""
    h, w = glyph_arr.shape[:2]
    dst_w, dst_h = size
    interpolation = cv2.INTER_AREA if (h >= dst_h and w >= dst_w) else cv2.INTER_LINEAR
    return cv2.resize(glyph_arr, size, interpolation=interpolation)


def _is_plus_sign(glyph_arr, threshold=TEMPLATE_PLUS_MIN_SCORE) -> bool:
    """Whether this glyph matches the '+' template class — checked as its
    own yes/no question rather than as one class among many (see
    _classify_number_run for why: detection only ever needs to know
    whether a result is positive, never what a negative sign actually
    looks like, and a genuine '-' reference has no equivalent to '+''s
    confirmed-hit verification). Measured directly: real '+' glyphs score
    ~1.0 here, every digit and the (unverified) '-' samples score <= 0.4,
    so this threshold has a wide, confirmed margin on both sides."""
    scores = _template_scores(glyph_arr)
    return float(scores[_TEMPLATE_PACK["is_plus"]].max()) >= threshold


def _classify_digit(glyph_arr):
    """Best-matching digit class (0-9 only — '+'/'-' are handled by
    _is_plus_sign, not here) + score, against every stored reference sample
    of every digit class."""
    scores = np.where(_TEMPLATE_PACK["is_digit"], _template_scores(glyph_arr), -np.inf)
    best = int(np.argmax(scores))
    if not np.isfinite(scores[best]):
        return None, -1.0                  # no digit templates loaded at all
    return str(_TEMPLATE_PACK["classes"][best]), float(scores[best])


def _split_into_number_runs(glyph_boxes):
    """Group glyph boxes into separate runs whenever the gap to the next
    one is much wider than a typical within-number gap — see
    TEMPLATE_GAP_RATIO."""
    if not glyph_boxes:
        return []
    widths = sorted(x1 - x0 for x0, x1, _y0, _y1 in glyph_boxes)
    gap_threshold = widths[len(widths) // 2] * TEMPLATE_GAP_RATIO
    runs = [[glyph_boxes[0]]]
    for prev_box, box in zip(glyph_boxes, glyph_boxes[1:]):
        if box[0] - prev_box[1] > gap_threshold:
            runs.append([])
        runs[-1].append(box)
    return runs


def _classify_number_run(run_boxes, mask_arr, img_height):
    """Classify one run of glyph boxes (already known to belong to a single
    number). Returns (value, confident):
      - (None, True): the sign confidently does NOT match '+' — a fast,
        confident "not a hit" that skips classifying the remaining digits
        entirely, since detection only ever needs to know whether a result
        is positive, never what a negative value actually reads as.
      - ("+<number>", True): the sign confidently matches '+' and every
        digit after it also classified confidently.
      - (None, False): genuine ambiguity (the sign is missing, or the sign
        or a digit didn't classify confidently) — the caller should fall
        back to Tesseract for this one read rather than trust a shaky
        result."""
    content_boxes = [b for b in run_boxes if not _is_comma_box(b, img_height)]
    if not content_boxes:
        return None, True     # nothing but commas: no number here, and nothing ambiguous about that
    x0, x1, y0, y1 = content_boxes[0]
    if not _is_plus_sign(mask_arr[y0:y1, x0:x1]):
        # "Not a '+'" is only a confident no-hit if this glyph is a SIGN at all.
        # A digit-height first glyph means the crop starts partway into the number
        # and the sign is missing — a positive value whose '+' got clipped looks
        # exactly like a negative one here. Reporting a confident "not a hit" for
        # that (as this did before) silently drops a real detection: no beep, no
        # Tesseract fallback, and no debug capture either, so the miss leaves no
        # trace anywhere to find it by later. Reproduced on 57 of 57 confirmed
        # hits by cropping the '+' off. Tesseract's own known weakness on this
        # font is dropping the leading '+' too, so the fallback is not guaranteed
        # to save it — but an uncertain read beats a silent one.
        if (y1 - y0) / img_height > TEMPLATE_SIGN_MAX_HEIGHT_FRAC:
            return None, False
        return None, True

    digits = []
    for box in content_boxes[1:]:
        x0, x1, y0, y1 = box
        cls, score = _classify_digit(mask_arr[y0:y1, x0:x1])
        if cls is None or score < TEMPLATE_MATCH_MIN_SCORE:
            return None, False
        digits.append(cls)
    if not digits:
        return None, False

    grouped = []
    for i, digit in enumerate(reversed(digits)):
        if i and i % 3 == 0:
            grouped.append(",")
        grouped.append(digit)
    return "+" + "".join(reversed(grouped)), True


def classify_number_via_templates(mask_img: Image.Image):
    """Try to read a "+<number>" out of *mask_img* using template matching.
    Returns (value_or_None, confident). confident=True means the result can
    be trusted outright with no need to also run Tesseract on this frame —
    true when a frame is confidently empty (zero glyphs found at all, the
    common case while idly watching for a popup), when a run's sign
    confidently isn't '+' (a negative/bad result — see
    _classify_number_run), and when a positive number was cleanly
    classified end to end. This is what keeps this fast rather than paying
    for both methods on every single read: an idle frame resolves near-
    instantly, a negative result stops at the sign check, and a real hit
    skips Tesseract entirely. confident=False only when
    segmentation/classification couldn't resolve a run cleanly (e.g.
    touching digits), so the caller should fall back to Tesseract for just
    that one read."""
    if not DIGIT_TEMPLATES:
        return None, False
    boxes = _segment_glyph_boxes(mask_img)
    if not boxes:
        return None, True
    mask_arr = np.array(mask_img)
    for run in _split_into_number_runs(boxes):
        # Every run gets a vote here, and a single "not confident" vote sends the
        # whole frame to Tesseract — so a run that cannot possibly hold a number
        # must be skipped outright rather than allowed to report ambiguity about
        # itself. See TEMPLATE_MIN_RUN_BOXES: one speck of border smudge in the
        # corner of the frame used to overrule the number run's own confident
        # verdict and cost a full Tesseract read on every single negative roll.
        if len(run) < TEMPLATE_MIN_RUN_BOXES:
            continue
        value, confident = _classify_number_run(run, mask_arr, mask_img.height)
        if not confident:
            return None, False
        if value is not None:
            return value, True
    return None, True


def _trim_to_text_band(mask_img: Image.Image, min_ink_frac=0.03, min_gap_rows=3, pad=4) -> Image.Image:
    """Keep only the tallest contiguous run of rows with enough ink to be a
    real text line, discarding anything separated from it by a mostly-blank
    gap. Confirmed against a real missed detection (see
    debug_captures/miss_20260914_232542_*.png): the auto-locate box's
    vertical padding (added earlier so it wouldn't clip digit tops/bottoms)
    was, in live gameplay, also catching a band of background texture below
    the actual number. That noise threw off Tesseract's layout analysis
    enough to make it drop the leading '+' entirely — cropping the noise
    out fixed it completely (confirmed: same image read as '+518,721'
    instead of '518,721' once the band below was removed)."""
    arr = np.array(mask_img)
    has_ink = (arr < 128).mean(axis=1) > min_ink_frac
    runs = []
    start, gap = None, 0
    for i, ink in enumerate(has_ink):
        if ink:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > min_gap_rows:
                runs.append((start, i - gap))
                start = None
    if start is not None:
        runs.append((start, len(has_ink) - 1))
    if not runs:
        return mask_img
    top, bottom = max(runs, key=lambda r: r[1] - r[0])
    top = max(0, top - pad)
    bottom = min(arr.shape[0], bottom + pad + 1)
    return mask_img.crop((0, top, arr.shape[1], bottom))


def preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """Upscale, then isolate the digits by color instead of just converting
    to plain grayscale — see OCR_MASK_VALUE_MIN/OCR_MASK_SATURATION_MAX for
    why that matters a lot for OCR reliability here. Scales toward
    OCR_TARGET_HEIGHT rather than a fixed multiplier, so a larger raw region
    (e.g. from a higher screen resolution) doesn't balloon processing time —
    see OCR_TARGET_HEIGHT for why that matters too. Finally trims to just
    the digit line — see _trim_to_text_band for why that matters too."""
    scale = OCR_TARGET_HEIGHT / img.height
    scale = max(OCR_SCALE_MIN, min(scale, OCR_SCALE_MAX))
    big = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    hsv = np.array(big.convert("HSV"))
    saturation, value = hsv[..., 1].astype(int), hsv[..., 2].astype(int)
    is_digit_ink = (value > OCR_MASK_VALUE_MIN) & (saturation < OCR_MASK_SATURATION_MAX)
    black_on_white = np.where(is_digit_ink, 0, 255).astype("uint8")
    return _trim_to_text_band(Image.fromarray(black_on_white, mode="L"))


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


_last_debug_capture = 0.0


def _maybe_save_debug_capture(raw_img, mask_img):
    """See DEBUG_CAPTURE_DIR above. Best-effort only — a failure to save a
    debug image must never break actual detection."""
    global _last_debug_capture
    now = time.perf_counter()
    if now - _last_debug_capture < DEBUG_CAPTURE_COOLDOWN:
        return
    _last_debug_capture = now
    try:
        DEBUG_CAPTURE_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        raw_img.save(DEBUG_CAPTURE_DIR / f"miss_{stamp}_raw.png")
        mask_img.save(DEBUG_CAPTURE_DIR / f"miss_{stamp}_mask.png")
    except Exception:
        pass


def save_success_capture(raw_img, value):
    """Companion to _maybe_save_debug_capture — misses alone don't teach
    anything without knowing what a correct read looks like too. Saves a
    CONFIRMED detection's raw crop labeled with the value that was read, so
    there's a growing set of known-correct examples to compare misses
    against (and eventually build digit templates from, not just diagnose
    failures). Not rate-limited: confirmed detections are already
    inherently infrequent — gated by the reset gameplay loop itself — so
    there's no flood risk here the way there is for the miss heuristic."""
    try:
        DEBUG_CAPTURE_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_value = value.replace(",", "")   # commas are awkward in filenames on some tools/OSes
        raw_img.save(DEBUG_CAPTURE_DIR / f"hit_{stamp}_{safe_value}.png")
    except Exception:
        pass


def read_delta(region, sct=None) -> tuple[str | None, Image.Image]:
    """Screenshot *region* and return (first "+<number>" match or None, the
    raw crop that produced it). The raw crop is returned alongside the
    value — not re-grabbed later by a caller that wants to log/save it —
    because a second, later screenshot can show something different if
    anything on screen changed in between, however briefly; a real
    confirmed detection got logged with a saved image of a completely
    different, mismatched number this way before this was fixed to return
    the actual source frame instead. When OCR produces text with digits in
    it that still doesn't parse as a "+<number>" — the likely signature of
    a real popup that OCR misread rather than plain empty background — the
    frame is saved via _maybe_save_debug_capture() for later recalibration.
    Tries template matching first (see classify_number_via_templates) and
    only falls back to Tesseract for a read it couldn't confidently
    resolve."""
    img = _grab(region, sct=sct)
    return read_delta_from_image(img), img


def read_delta_from_image(img: Image.Image, save_debug=True) -> str | None:
    """The actual read, split out from read_delta() so the startup capture
    re-check (see check_pending_captures) scores saved frames through the
    exact same path live detection uses — a re-check that reimplemented the
    read would drift from the real one and start reporting on code that
    isn't running.

    save_debug=False is not optional for that caller but load-bearing: this
    function saves garbled frames via _maybe_save_debug_capture(), so
    letting it fire while re-reading saved captures would have every
    unreadable capture spawn a fresh copy of itself, which the next launch
    would then re-check and copy again."""
    proc = preprocess_for_ocr(img)

    value, confident = classify_number_via_templates(proc)
    if confident:
        return value

    text = pytesseract.image_to_string(proc, config=OCR_CONFIG)
    cleaned = text.replace(" ", "")
    m = PLUS_NUMBER_RE.search(cleaned)
    if not m and save_debug and re.search(r'\d{2,}', cleaned):
        _maybe_save_debug_capture(img, proc)
    return m.group(0) if m else None


def _load_check_ledger() -> dict:
    """See CAPTURE_CHECK_LEDGER. A missing or corrupt ledger is treated as an
    empty one rather than an error — the worst case is re-checking captures
    that were already judged, which costs time and changes nothing, while
    refusing to start would take the app down over a side feature."""
    try:
        data = json.loads(CAPTURE_CHECK_LEDGER.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("checked"), dict):
            return data
    except Exception:
        pass
    return {"cutoff": CAPTURE_CHECK_CUTOFF, "runs": [], "checked": {}}


def _save_check_ledger(ledger: dict):
    try:
        DEBUG_CAPTURE_DIR.mkdir(exist_ok=True)
        CAPTURE_CHECK_LEDGER.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    except Exception:
        pass


def _pending_captures(ledger: dict) -> list:
    """Captures stamped on/after CAPTURE_CHECK_CUTOFF that the ledger hasn't
    judged yet — in practice the ones the previous run produced, since every
    launch clears its own backlog. Sorted by name, which for these filenames
    is chronological."""
    try:
        names = sorted(p.name for p in DEBUG_CAPTURE_DIR.glob("*.png"))
    except Exception:
        return []
    pending = []
    for name in names:
        if name in ledger["checked"] or name in KNOWN_BAD_CAPTURES:
            continue
        m = HIT_CAPTURE_RE.match(name) or MISS_CAPTURE_RE.match(name)
        if m and m.group(1) >= CAPTURE_CHECK_CUTOFF:
            pending.append(name)
    return pending


def check_pending_captures(on_progress=None) -> dict:
    """Re-read every not-yet-judged capture with the current pipeline and
    record what it now says. Returns a summary dict of the counts below.

    hit_*.png files are scored, because the value in the filename is a
    confirmed read: matching it is 'correct', reading something else or
    nothing at all is a regression worth knowing about. miss_*_raw.png files
    have no such label — nobody ever recorded what they should have said —
    so they are only split into 'recovered' (the current code finds a
    +<number> where the code at capture time found nothing) and 'still
    unread', which is the number that should fall as the classifier
    improves."""
    ledger = _load_check_ledger()
    pending = _pending_captures(ledger)
    summary = {"checked": 0, "hit_correct": 0, "hit_wrong": 0,
               "miss_recovered": 0, "miss_unread": 0, "unreadable": 0,
               "pending": len(pending)}
    if not pending:
        return summary

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for i, name in enumerate(pending, 1):
        hit = HIT_CAPTURE_RE.match(name)
        try:
            with Image.open(DEBUG_CAPTURE_DIR / name) as img:
                read = read_delta_from_image(img.convert("RGB"), save_debug=False)
        except Exception:
            # A capture that can't even be opened (half-written when the last run
            # was killed, say) gets marked so it isn't retried every launch forever.
            ledger["checked"][name] = {"error": "unreadable", "at": stamp}
            summary["unreadable"] += 1
            summary["checked"] += 1
            continue

        entry = {"read": read, "at": stamp}
        if hit:
            # The filename strips commas (see save_success_capture) but the
            # classifier groups them back in, so compare without them.
            expected = hit.group(2)
            entry["expected"] = expected
            entry["ok"] = bool(read) and read.replace(",", "") == expected
            summary["hit_correct" if entry["ok"] else "hit_wrong"] += 1
        else:
            entry["recovered"] = read is not None
            summary["miss_recovered" if read else "miss_unread"] += 1

        ledger["checked"][name] = entry
        summary["checked"] += 1
        if on_progress:
            on_progress(i, len(pending))
        if summary["checked"] % CAPTURE_CHECK_FLUSH == 0:
            _save_check_ledger(ledger)
        time.sleep(CAPTURE_CHECK_YIELD)

    ledger["cutoff"] = CAPTURE_CHECK_CUTOFF
    ledger.setdefault("runs", []).append({"at": stamp, **summary})
    _save_check_ledger(ledger)
    return summary


def format_check_summary(s: dict) -> str:
    if not s["checked"]:
        return "  Capture re-check: nothing new since the last run."
    parts = [f"{s['checked']} new capture(s) re-read"]
    if s["hit_correct"] or s["hit_wrong"]:
        parts.append(f"confirmed hits: {s['hit_correct']} still correct, {s['hit_wrong']} now wrong")
    if s["miss_recovered"] or s["miss_unread"]:
        parts.append(f"misses: {s['miss_recovered']} now readable, {s['miss_unread']} still unread")
    if s["unreadable"]:
        parts.append(f"{s['unreadable']} unreadable file(s)")
    return "  Capture re-check: " + "; ".join(parts) + "."


def start_capture_check(on_done=None):
    """Kick the re-check off on a daemon thread at startup. Deliberately not
    blocking: the first launch after this shipped has a backlog of thousands
    of images (~24ms each) to work through, and making the app unusable for a
    minute and a half to grade old screenshots would be a bad trade for a
    diagnostic. Later launches only see their predecessor's captures, so this
    normally finishes in seconds. Daemon so closing the app doesn't wait for
    it — the ledger is flushed as it goes, so a partial scan is not lost."""
    def _run():
        try:
            summary = check_pending_captures()
        except Exception as exc:          # never let a diagnostic take detection down
            print(f"  Capture re-check failed: {exc}")
            return
        print(format_check_summary(summary))
        if on_done:
            on_done(summary)

    threading.Thread(target=_run, daemon=True, name="capture-check").start()


def smooth_move_to(target_x, target_y, duration=AUTOLOCATE_MOVE_DURATION, steps=AUTOLOCATE_MOVE_STEPS,
                    jitter_px=AUTOLOCATE_MOVE_JITTER_PX):
    """Glide the cursor to (target_x, target_y) instead of teleporting —
    pydirectinput.moveTo's own duration/tween arguments are accepted but
    silently ignored (its implementation always jumps instantly), so this is
    done by hand: ease-in-out timing (slow to start, fastest in the middle,
    slow to settle) plus a little random sideways wobble off the straight
    line — shrinking to zero as it nears the target so it still lands
    precisely — reads as a human hand rather than a robotic slide."""
    start_x, start_y = pydirectinput.position()
    dx, dy = target_x - start_x, target_y - start_y
    dist = math.hypot(dx, dy)
    perp_x, perp_y = (-dy / dist, dx / dist) if dist > 0 else (0.0, 0.0)
    for i in range(1, steps + 1):
        t = i / steps
        eased = (1 - math.cos(math.pi * t)) / 2
        wobble = random.uniform(-1, 1) * jitter_px * (1 - t)
        x = round(start_x + dx * eased + perp_x * wobble)
        y = round(start_y + dy * eased + perp_y * wobble)
        pydirectinput.moveTo(x, y)
        time.sleep(duration / steps)


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


def default_region() -> tuple:
    """A harmless starting box for first launch (nothing saved yet), so the
    control window opens immediately instead of blocking behind a full-screen
    click-drag prompt — the user sets the real region afterward via 'Choose
    region' (F8) or 'Auto-locate' (F7), both of which stay inside the app."""
    with mss.MSS() as sct:
        desktop = sct.monitors[0]
    w, h = 200, 60
    x = desktop["left"] + (desktop["width"] - w) // 2
    y = desktop["top"] + (desktop["height"] - h) // 2
    return (x, y, w, h)

# ── Persistent overlay rectangle ────────────────────────────────────────────

class OverlayApp:
    """
    A violet detection box with a click-through center and four resize handles.
    F8 opens a new selection. A background thread reads the selected region.
    """
    BORDER      = 12
    COLOR       = UI_ACCENT
    COLOR_HIT   = UI_PRIMARY
    HANDLE_SIZE = 20
    MIN_SIZE    = 20

    def __init__(self, region: tuple, enter_spam: bool = True,
                 enter_hotkey: str = ENTER_HOTKEY,
                 enter_interval: float = ENTER_INTERVAL,
                 click_interval: float = CLICK_INTERVAL,
                 beep_hotkey: str = BEEP_HOTKEY,
                 min_read_gap: float = MIN_READ_GAP,
                 is_placeholder_region: bool = False):
        self.region      = list(region)   # [x, y, w, h] — mutable, moved/resized live
        self._is_placeholder_region = is_placeholder_region   # True until the user sets a real region
        self.status_text = "paused"
        self.hit          = False
        self._prev_hit    = False   # edge-detect found/not-found so beep+stop fires once per detection
        self.last_value    = None
        self.last_beep_time = 0.0
        self.min_read_gap  = min_read_gap
        self.root         = None
        self._canvas      = None
        self._rect_id     = None
        self._wave_ids    = []
        self._wave_phase  = 0.0
        self._handle_ids  = {}
        self._drag        = {}
        self._ocr_thread  = None
        self.overlay       = None
        self._status_label = None
        self._detail_label = None
        self._start_button = None
        self._mute_button = None
        self._record_button = None
        self._activity_list = None
        self._activity_pending = queue.SimpleQueue()
        self._activity_lines = []
        self._start_after = None
        self._ocr_available = True
        self._video_player = None
        self._video_panel = None
        self._video_after = None
        self._video_image = None

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
        self._toggle_requests = queue.SimpleQueue()
        self._selection_requested = threading.Event()
        self._selector = None
        self._selecting = False
        self._region_revision = 0
        self._autolocate_requested = threading.Event()
        self._label_template_gray = None
        self._autolocate_available = True
        self._box_visible = True
        self._box_toggle_button = None
        self._activation_target = None
        self.discord = DiscordNotifier(log=self._log)
        self._discord_button = None
        self._discord_settings_button = None
        self._mute_var = None
        self._discord_var = None

    def _build_window(self):
        """Build the normal taskbar window plus the capture-safe screen overlay."""
        root = tk.Tk()
        root.title("Maple Tapper Looker")
        with Image.open(Path(__file__).parent / "assets" / "logo.png") as image:
            self._logo_image = ImageTk.PhotoImage(
                image.resize((144, 144), Image.Resampling.LANCZOS), master=root)
            self._icon_image = ImageTk.PhotoImage(
                image.resize((256, 256), Image.Resampling.LANCZOS), master=root)
        root.iconphoto(True, self._icon_image)
        root.resizable(False, False)
        root.configure(bg=UI_BG)
        root.geometry("540x610")
        root.minsize(500, 560)
        root.protocol("WM_DELETE_WINDOW", self._quit)
        root.option_add("*Font", ("Segoe UI", 10))

        outer = tk.Frame(root, bg=UI_BG, padx=24, pady=20)
        outer.pack(side="left", fill="both", expand=True)
        header = tk.Frame(outer, bg=UI_BG)
        header.pack(fill="x", pady=(0, 14))
        tk.Label(header, image=self._logo_image, bg=UI_BG).pack(pady=(0, 10))
        heading = tk.Frame(header, bg=UI_BG)
        heading.pack()
        tk.Label(heading, text="MAPLE / TAPPER LOOKER", fg=UI_TEXT, bg=UI_BG,
                 font=("Segoe UI", 16, "bold"), anchor="center").pack(fill="x")
        tk.Label(heading, text="OCR and input automation control", fg=UI_MUTED, bg=UI_BG,
                 font=("Segoe UI", 10), anchor="center").pack(fill="x", pady=(4, 0))
        accent = tk.Frame(outer, bg=UI_BORDER, height=2)
        accent.pack(pady=(0, 18))
        tk.Frame(accent, bg=UI_PRIMARY, width=80, height=2).pack(side="left")
        tk.Frame(accent, bg=UI_ACCENT, width=40, height=2).pack(side="left")

        status = self._card(outer)
        status.pack(fill="x")
        self._status_label = tk.Label(status, text="PAUSED", fg=UI_PRIMARY, bg=UI_SURFACE,
                                      font=("Segoe UI", 15, "bold"), anchor="center")
        self._status_label.pack(fill="x", padx=16, pady=(14, 2))
        self._detail_label = tk.Label(status, text="Press Start or F9 to begin detection.",
                                      fg=UI_MUTED, bg=UI_SURFACE, anchor="center", justify="center",
                                      wraplength=450)
        self._detail_label.pack(fill="x", padx=16, pady=(0, 12))
        buttons = tk.Frame(status, bg=UI_SURFACE)
        buttons.pack(padx=16, pady=(0, 14))
        self._start_button = self._button(buttons, f"Start watching  ({self.enter_hotkey.upper()})", self._start_or_stop,
                                          UI_PRIMARY, UI_BG)
        self._start_button.pack(side="left")
        self._button(buttons, "Choose region  (F8)", self._request_selection,
                     UI_BUTTON, UI_TEXT).pack(side="left", padx=(10, 0))
        self._button(buttons, "Auto-locate  (F7)", self._request_autolocate,
                     UI_BUTTON, UI_TEXT).pack(side="left", padx=(10, 0))
        self._box_toggle_button = self._button(buttons, "Hide box", self._toggle_box_visibility,
                                               UI_BUTTON, UI_TEXT)
        self._box_toggle_button.pack(side="left", padx=(10, 0))
        tk.Label(status, text="Auto-locate currently only recognizes the Combat Power reset dialog.",
                 fg=UI_MUTED, bg=UI_SURFACE, font=("Segoe UI", 8), anchor="center",
                 justify="center", wraplength=450).pack(fill="x", padx=16, pady=(0, 12))

        audio = self._card(outer)
        audio.pack(fill="x", pady=(14, 0))
        tk.Label(audio, text="Detection audio", fg=UI_TEXT, bg=UI_SURFACE,
                 font=("Segoe UI", 12, "bold"), anchor="center").pack(fill="x", padx=16, pady=(14, 3))
        tk.Label(audio, text="Record a message or use the built-in beep.", fg=UI_MUTED,
                 bg=UI_SURFACE, anchor="center").pack(fill="x", padx=16, pady=(0, 10))
        audio_buttons = tk.Frame(audio, bg=UI_SURFACE)
        audio_buttons.pack(padx=16, pady=(0, 14))
        self._record_button = self._button(audio_buttons, "Record  (F11)",
                                           lambda: self._request_audio(self.alert.toggle), UI_BUTTON, UI_TEXT)
        self._record_button.pack(side="left")
        self._mute_images = []
        for selected in (False, True):
            image = Image.new("RGB", (168, 84), UI_SURFACE)
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((0, 0, 167, 83), radius=42,
                                   fill=UI_PRIMARY if selected else UI_BORDER)
            x = 87 if selected else 6
            draw.ellipse((x, 6, x + 75, 77), fill="white")
            self._mute_images.append(ImageTk.PhotoImage(
                image.resize((56, 28), Image.Resampling.LANCZOS), master=root))
        self._mute_var = tk.BooleanVar(root, value=False)
        self._mute_button = tk.Checkbutton(
            audio_buttons, text="Mute  (F10)", command=self._toggle_beep, variable=self._mute_var,
            image=self._mute_images[0], selectimage=self._mute_images[1],
            indicatoron=False, compound="left", relief="flat", offrelief="flat",
            borderwidth=0, bg=UI_SURFACE, fg=UI_TEXT, selectcolor=UI_SURFACE,
            activebackground=UI_SURFACE, activeforeground=UI_TEXT,
            highlightbackground=UI_SURFACE, highlightcolor=UI_PRIMARY,
            highlightthickness=1, takefocus=True, padx=6, pady=5, cursor="hand2")
        self._mute_button.pack(side="left", padx=8)
        self._button(audio_buttons, "Reset  (F12)", lambda: self._request_audio(self.alert.reset),
                     UI_BUTTON, UI_TEXT).pack(side="left")

        discord = self._card(outer)
        discord.pack(fill="x", pady=(14, 0))
        tk.Label(discord, text="Discord alerts", fg=UI_TEXT, bg=UI_SURFACE,
                 font=("Segoe UI", 12, "bold"), anchor="center").pack(fill="x", padx=16, pady=(14, 3))
        tk.Label(discord, text="Posts a server-channel mention and can include game chat. Not a DM or guaranteed notification.",
                 fg=UI_MUTED, bg=UI_SURFACE, anchor="center", justify="center", wraplength=450).pack(
                     fill="x", padx=16, pady=(0, 10))
        discord_buttons = tk.Frame(discord, bg=UI_SURFACE)
        discord_buttons.pack(padx=16, pady=(0, 14))
        self._discord_var = tk.BooleanVar(root, value=self.discord.enabled)
        self._discord_button = tk.Checkbutton(
            discord_buttons, text="Discord alerts", command=self._toggle_discord, variable=self._discord_var,
            image=self._mute_images[0], selectimage=self._mute_images[1], indicatoron=False,
            compound="left", relief="flat", offrelief="flat", borderwidth=0, bg=UI_SURFACE,
            fg=UI_TEXT, selectcolor=UI_SURFACE, activebackground=UI_SURFACE,
            activeforeground=UI_TEXT, highlightbackground=UI_SURFACE, highlightcolor=UI_PRIMARY,
            highlightthickness=1, takefocus=True, padx=6, pady=5, cursor="hand2",
            state="normal" if self.discord.supported else "disabled")
        self._discord_button.pack(side="left")
        self._discord_settings_button = self._button(discord_buttons, "Discord settings", self._open_discord_settings,
                                                      UI_BUTTON, UI_TEXT)
        self._discord_settings_button.pack(side="left", padx=(8, 0))
        if not self.discord.supported:
            self._log("Discord alerts need Windows DPAPI and are unavailable here.")

        self._bored_button = self._button(outer, "i'm bored", self._open_video,
                                           UI_BUTTON, UI_TEXT)
        self._bored_button.pack(pady=(14, 0))

        activity = self._card(outer)
        activity.pack(fill="both", expand=True, pady=(14, 0))
        tk.Label(activity, text="Recent activity", fg=UI_TEXT, bg=UI_SURFACE,
                 font=("Segoe UI", 12, "bold"), anchor="center").pack(fill="x", padx=16, pady=(14, 6))
        self._activity_list = tk.Text(activity, bg=UI_SURFACE, fg=UI_MUTED,
                                      selectbackground=UI_BORDER, selectforeground=UI_TEXT,
                                      highlightthickness=0, borderwidth=0, wrap="word",
                                      height=6, width=1, font=("Segoe UI", 10), state="disabled")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar", background=UI_BUTTON,
                        troughcolor=UI_SURFACE, arrowcolor=UI_TEXT,
                        bordercolor=UI_SURFACE, lightcolor=UI_BUTTON, darkcolor=UI_BUTTON)
        style.map("Vertical.TScrollbar", background=[("active", UI_PRIMARY_ACTIVE)])
        scroll = ttk.Scrollbar(activity, command=self._activity_list.yview)
        scroll.pack(side="right", fill="y", pady=(0, 12))
        self._activity_list.config(yscrollcommand=scroll.set)
        self._activity_list.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        if self._is_placeholder_region:
            self._log("No saved region yet — press 'Choose region' (F8) or 'Auto-locate' (F7) to set one.")
        else:
            self._log("Ready. Select the number region, then start watching.")

        overlay = tk.Toplevel(root)
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.attributes("-transparentcolor", "black")
        overlay.configure(bg="black")
        overlay.resizable(False, False)
        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
        canvas.pack()
        self._rect_id = canvas.create_rectangle(0, 0, 0, 0, outline=self.COLOR,
                                                 width=self.BORDER * 2, fill="black", tags="border")
        self._wave_ids = [canvas.create_line(0, 0, 0, 0, fill=self.COLOR,
                                             width=self.BORDER * 2, tags="border")
                          for _ in range(64)]
        for corner in ("nw", "ne", "sw", "se"):
            self._handle_ids[corner] = canvas.create_rectangle(0, 0, 0, 0, fill=self.COLOR,
                                                                 outline="", tags=("handle", f"handle_{corner}"))
        canvas.tag_bind("border", "<ButtonPress-1>", self._on_move_press)
        canvas.tag_bind("border", "<B1-Motion>", self._on_move_drag)
        canvas.tag_bind("border", "<ButtonRelease-1>", self._on_release)
        for corner in ("nw", "ne", "sw", "se"):
            canvas.tag_bind(f"handle_{corner}", "<ButtonPress-1>",
                            lambda e, c=corner: self._on_resize_press(e, c))
            canvas.tag_bind(f"handle_{corner}", "<B1-Motion>", self._on_resize_drag)
            canvas.tag_bind(f"handle_{corner}", "<ButtonRelease-1>", self._on_release)
        self.root, self.overlay, self._canvas = root, overlay, canvas
        self._sync_geometry()
        root.update_idletasks()
        width = max(540, root.winfo_reqwidth())
        height = max(610, root.winfo_reqheight())
        root.geometry(f"{width}x{height}")
        root.minsize(width, height)
        return root

    @staticmethod
    def _card(parent):
        return tk.Frame(parent, bg=UI_SURFACE, highlightbackground=UI_BORDER,
                        highlightthickness=1)

    @staticmethod
    def _button(parent, text, command, bg, fg):
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=UI_PRIMARY_ACTIVE, activeforeground=UI_BG,
                         highlightbackground=UI_BORDER, highlightcolor=UI_ACCENT,
                         highlightthickness=1,
                         disabledforeground=UI_MUTED, relief="flat", bd=0, padx=14, pady=10,
                         cursor="hand2", font=("Segoe UI", 10, "bold"), takefocus=True)

    def _log(self, message):
        self._activity_pending.put(f"{time.strftime('%H:%M:%S')}  {message}")

    def _open_video(self):
        if self._video_panel is not None:
            return
        directory = _BASE_DIR / "videos"
        if getattr(sys, "frozen", False) and not directory.is_dir():
            directory = Path(__file__).parent / "videos"
        try:
            videos = [path for path in directory.iterdir()
                      if path.is_file() and path.suffix.lower() in
                      {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}]
            if not videos:
                self._log(f"No videos found in {directory}.")
                return
            from ffpyplayer.player import MediaPlayer
            path = random.choice(videos)
            events = queue.SimpleQueue()
            self._video_events = events
            self._video_player = MediaPlayer(
                str(path), ff_opts={"volume": 1.0, "out_fmt": "rgb24"},
                callback=lambda kind, value: events.put((kind, value)))
            self._video_started = time.monotonic()
            self._video_has_frame = False
            self.root.update_idletasks()
            self._video_base_size = (self.root.winfo_width(), self.root.winfo_height())
            self._video_panel = self._card(self.root)
            self._video_panel.configure(width=480)
            self._video_panel.pack(side="right", fill="y")
            self._video_panel.pack_propagate(False)
            self._button(self._video_panel, "Close video", self._close_video,
                         UI_BUTTON, UI_TEXT).pack(pady=12)
            tk.Label(self._video_panel, text=path.name, wraplength=440,
                     fg=UI_TEXT, bg=UI_SURFACE).pack(padx=12)
            self._video_label = tk.Label(self._video_panel, text="Loading video…",
                                         fg=UI_TEXT, bg="black")
            self._video_label.pack(fill="both", expand=True, padx=12, pady=12)
            width, height = self._video_base_size
            self.root.geometry(f"{width + 480}x{height}")
            self._bored_button.config(state="disabled")
            self._log(f"Playing: {path.name}")
            self._video_tick()
        except Exception as exc:
            self._log(f"Cannot play video: {exc}")
            self._close_video()

    def _video_tick(self):
        self._video_after = None
        try:
            while not self._video_events.empty():
                kind, value = self._video_events.get_nowait()
                if kind.endswith(":error") or (kind == "read:exit" and value):
                    raise RuntimeError(value or kind)
            frame, delay = self._video_player.get_frame()
            if delay == "eof":
                self._close_video()
                return
            if frame is not None:
                self._video_has_frame = True
                image, _ = frame
                picture = Image.frombytes("RGB", image.get_size(), bytes(image.to_bytearray()[0]))
                picture = ImageOps.contain(
                    picture, (max(1, self._video_label.winfo_width()),
                              max(1, self._video_label.winfo_height())), Image.Resampling.LANCZOS)
                self._video_image = ImageTk.PhotoImage(picture, master=self.root)
                self._video_label.config(image=self._video_image, text="")
            elif not self._video_has_frame and time.monotonic() - self._video_started > 10:
                raise RuntimeError("No video frames received within 10 seconds.")
            self._video_after = self.root.after(
                max(10, min(100, int(delay * 1000))) if isinstance(delay, (int, float)) else 30,
                self._video_tick)
        except Exception as exc:
            self._log(f"Video playback failed: {exc}")
            self._close_video()

    def _close_video(self):
        if self._video_after is not None:
            self.root.after_cancel(self._video_after)
            self._video_after = None
        if self._video_player is not None:
            self._video_player.close_player()
            self._video_player = None
        if self._video_panel is not None:
            self._video_panel.destroy()
            self._video_panel = None
            width, height = self._video_base_size
            self.root.geometry(f"{width}x{height}")
            self._bored_button.config(state="normal")
        self._video_image = None

    def _request_selection(self):
        self._selection_requested.set()

    def _request_autolocate(self):
        self._autolocate_requested.set()

    def _toggle_box_visibility(self):
        """Show/hide the on-screen overlay box, purely visual — OCR keeps
        reading self.region either way, since it screenshots the region
        directly and never depends on the overlay window being shown."""
        self._box_visible = not self._box_visible
        if self._box_visible and not self._selecting:
            self.overlay.deiconify()
        else:
            self.overlay.withdraw()
        self._box_toggle_button.config(text="Show box" if not self._box_visible else "Hide box")

    def _request_audio(self, action):
        self._audio_requests.put(action)

    def _start_or_stop(self):
        if not self._ocr_available or self._selecting:
            return
        if self.enter_on or self._start_after is not None:
            self._cancel_start()
            self._set_enter_on(False)
            self.status_text = "paused"
            self._log("Detection paused.")
            return
        self._countdown_start(3)

    def _countdown_start(self, seconds):
        self.status_text = f"Starting in {seconds}… click your game target now."
        self._log(self.status_text)
        self._start_button.config(text=f"Cancel start  ({self.enter_hotkey.upper()})")
        self._start_after = self.root.after(1000, self._finish_countdown, seconds - 1)

    def _finish_countdown(self, seconds):
        self._start_after = None
        if seconds:
            self._countdown_start(seconds)
            return
        if self.enter_spam_enabled and self.root.focus_displayof() is not None:
            self.status_text = "Start cancelled. Focus the game, then press F9."
            self._log(self.status_text)
            return
        self.hit = self._prev_hit = False
        self.last_value = None
        self._set_enter_on(True)
        if self.enter_on:
            self.status_text = "watching…"
            self._log("Detection started.")

    def _cancel_start(self):
        if self._start_after is not None:
            self.root.after_cancel(self._start_after)
            self._start_after = None

    def _sync_geometry(self):
        x, y, w, h = self.region
        b, hs = self.BORDER, self.HANDLE_SIZE
        W, H = w + b * 2, h + b * 2
        self.overlay.geometry(f"{W}x{H}+{x - b}+{y - b}")
        self._canvas.config(width=W, height=H)
        self._canvas.coords(self._rect_id, 0, 0, W - 1, H - 1)
        points = ((0, 0), (W - 1, 0), (W - 1, H - 1), (0, H - 1), (0, 0))
        for i, item in enumerate(self._wave_ids):
            side, step = divmod(i, 16)
            x1, y1 = points[side]
            x2, y2 = points[side + 1]
            dx, dy = (x2 - x1) / 16, (y2 - y1) / 16
            self._canvas.coords(item, x1 + step * dx, y1 + step * dy,
                                x1 + (step + 1) * dx, y1 + (step + 1) * dy)
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
        self.overlay.geometry(f"+{self.region[0] - b}+{self.region[1] - b}")

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
        self._cancel_start()
        self._region_revision += 1
        self._selecting = True
        self._set_enter_on(False)
        self._unlock_mouse()
        self.hit = self._prev_hit = False
        self.last_value = None
        self.status_text = "Drag to select a region"
        self._drag = {}
        self.overlay.withdraw()
        if self._selector is not None:
            self._selector.reset()
        else:
            self._selector = RegionSelector(parent=self.root, on_done=self._finish_selection)

    def _finish_selection(self, region):
        if region is not None:
            self.region = list(region)
            save_region(region)
            self._is_placeholder_region = False
        self._selector = None
        self._region_revision += 1
        self.hit = self._prev_hit = False
        self.last_value = None
        self.status_text = "paused"
        self._log("Region selected. Press Start or F9 to begin.")
        self._sync_geometry()
        self.root.deiconify()
        if self._box_visible:
            self.overlay.deiconify()
        self._selecting = False

    def _quit(self):
        print("\nStopped.")
        self._running = False
        self.discord.close()
        target = getattr(self, "_activation_target", None)
        if target is not None and hasattr(target, "close"):
            target.close()
        self._unlock_mouse()
        self.alert.close()
        self._close_video()
        hotkeys = ["f7", "f8", "f11", "f12", self.beep_hotkey, self.enter_hotkey]
        for hk in hotkeys:
            try:
                keyboard.remove_hotkey(hk)
            except (KeyError, ValueError):
                pass
        self._cancel_start()
        if self.overlay is not None:
            self.overlay.destroy()
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
        if value and (self._selecting or not self._ocr_available):
            return
        if value and self._is_placeholder_region:
            self.status_text = "No region set — press 'Choose region' (F8) or 'Auto-locate' (F7) first."
            self._log(self.status_text)
            return
        if (value and self.enter_spam_enabled and self.root is not None
                and self.root.focus_displayof() is not None):
            self.status_text = "Focus the game, then press F9."
            self._log(self.status_text)
            return
        if value == self.enter_on:
            return
        self.enter_on = value
        self._activation += 1
        if value:
            previous = getattr(self, "_activation_target", None)
            if previous is not None and hasattr(previous, "close"):
                previous.close()
            self._activation_target = None
            own_hwnds = []
            for window in (self.root, self.overlay):
                try:
                    own_hwnds.append(window.winfo_id())
                except (AttributeError, tk.TclError):
                    pass
            self._activation_target = bind_foreground_target(
                self.region, own_hwnds, report=self._log if self.discord.enabled else None)
            if self.discord.enabled and self._activation_target is None:
                self._log("Discord images unavailable for this activation; text alerts still work.")
        if value and self.enter_spam_enabled:
            self._lock_mouse()
        else:
            self._unlock_mouse()

    def _toggle_enter_spam(self):
        """Toggle OCR and automation. Start each activation with a fresh read."""
        if not self._ocr_available or self._selecting:
            return
        if self._start_after is not None:
            self._cancel_start()
            self.status_text = "paused"
            return
        self.hit = False
        self._prev_hit = False
        self.last_value = None
        requested = not self.enter_on
        self._set_enter_on(requested)
        if self.enter_on == requested:
            self.status_text = "watching…" if self.enter_on else "paused"
        self._log(f"F9: {'started' if self.enter_on else 'paused'}.")

    def _toggle_beep(self):
        """Hotkey callback — mutes/unmutes the detection beep, independent of
        the Enter+Click spam state."""
        self.beep_enabled = not self.beep_enabled
        self._log(f"Audio {'unmuted' if self.beep_enabled else 'muted'}.")

    def _toggle_discord(self):
        requested = not self.discord.enabled
        try:
            if not self.discord.set_enabled(requested):
                self._log("Set Discord webhook and user ID before enabling alerts.")
                return
        except OSError:
            self._log("Could not save Discord settings.")
        else:
            self._log(f"Discord alerts {'enabled' if requested else 'disabled'}.")
        finally:
            if self._discord_var is not None:
                self._discord_var.set(self.discord.enabled)

    def _open_discord_settings(self):
        if not self.discord.supported:
            return
        current = self.discord.settings
        dialog = tk.Toplevel(self.root)
        dialog.title("Discord settings")
        dialog.configure(bg=UI_BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        body = tk.Frame(dialog, bg=UI_BG, padx=20, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Posts to a server channel, mentions one user, and can include game chat. It is not a DM.",
                 bg=UI_BG, fg=UI_MUTED, justify="left", wraplength=420).pack(fill="x", pady=(0, 12))
        tk.Label(body, text="Webhook URL", bg=UI_BG, fg=UI_TEXT, anchor="w").pack(fill="x")
        webhook = tk.Entry(body, width=54, show="•")
        webhook.pack(fill="x", pady=(2, 8))
        webhook.insert(0, current.webhook_url if current else "")
        tk.Label(body, text="Discord user ID", bg=UI_BG, fg=UI_TEXT, anchor="w").pack(fill="x")
        user_id = tk.Entry(body, width=54)
        user_id.pack(fill="x", pady=(2, 4))
        user_id.insert(0, current.user_id if current else "")
        error = tk.Label(body, text="", bg=UI_BG, fg=UI_PRIMARY_ACTIVE, anchor="w")
        error.pack(fill="x", pady=(0, 8))
        buttons = tk.Frame(body, bg=UI_BG)
        buttons.pack()

        def save():
            try:
                self.discord.save_settings(webhook.get(), user_id.get())
            except ValueError as exc:
                error.config(text=str(exc))
                return
            except OSError:
                error.config(text="Could not save settings. Previous settings were kept.")
                return
            self._log("Discord settings saved.")
            dialog.destroy()

        def clear():
            try:
                self.discord.clear()
            except OSError:
                error.config(text="Could not clear settings.")
                return
            if self._discord_var is not None:
                self._discord_var.set(False)
            self._log("Discord settings cleared and disabled.")
            dialog.destroy()

        def test_alert():
            if self.discord.settings is None:
                error.config(text="Save valid settings before sending a test alert.")
                return
            if not self.discord.schedule("", test=True):
                error.config(text="A Discord alert is already pending.")

        self._button(buttons, "Save", save, UI_PRIMARY, UI_BG).pack(side="left")
        self._button(buttons, "Cancel", dialog.destroy, UI_BUTTON, UI_TEXT).pack(side="left", padx=8)
        self._button(buttons, "Clear settings", clear, UI_BUTTON, UI_TEXT).pack(side="left")
        self._button(buttons, "Send test alert", test_alert, UI_BUTTON, UI_TEXT).pack(side="left", padx=(8, 0))
        webhook.focus_set()

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
            self._set_enter_on(False)
            self.status_text = "OCR error - see activity"
            self._log(f"OCR stopped: {e}")
            return None, None

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
                if self._selecting or not self.enter_on or not self._ocr_available:
                    time.sleep(RENDER_INTERVAL)
                    continue
                revision = self._region_revision
                activation = self._activation
                t0 = time.perf_counter()
                value, _raw_img = self._read_with_retry(sct)
                if (self._selecting or revision != self._region_revision
                        or not self.enter_on or activation != self._activation):
                    continue

                if value and not self._prev_hit:
                    # New detection (edge-triggered, so a popup that stays on
                    # screen for several reads doesn't re-trigger this every
                    # frame). Stop the spam immediately so it doesn't click
                    # through the popup, then take independent follow-up
                    # reads before believing it — a single stray OCR frame
                    # was producing occasional false positives, so requiring
                    # a confirming read earns the beep. Retries up to
                    # CONFIRM_ATTEMPTS times rather than requiring only the
                    # very next read to also succeed: spam is already
                    # stopped at this point (self.hit is True), so retrying
                    # costs nothing but a little time, whereas requiring
                    # exactly one immediate follow-up was discarding real
                    # detections — and resuming spam through the still-
                    # visible popup — whenever OCR missed just that one
                    # retry (e.g. a fade-in animation frame).
                    self.hit = True
                    self._unlock_mouse()
                    self.status_text = "confirming…"

                    confirm = None
                    confirm_img = None
                    aborted = False
                    for _ in range(CONFIRM_ATTEMPTS):
                        confirm, confirm_img = self._read_with_retry(sct)
                        if (self._selecting or revision != self._region_revision
                                or not self.enter_on or activation != self._activation):
                            aborted = True
                            break
                        if confirm:
                            break
                    if aborted:
                        continue
                    if confirm:
                        value = confirm
                        self.status_text = value
                        self.last_value = value
                        self._log(f"Detected {value}.")
                        target = self._activation_target
                        self._set_enter_on(False)
                        try:
                            save_success_capture(confirm_img, value)
                        except Exception:
                            pass
                        if self.discord.enabled:
                            if target is not None and not target_is_current(target):
                                target = None
                            if not self.discord.schedule(value, target, activation=activation):
                                self._log("Discord alert skipped.")
                        if self.beep_enabled:
                            now = time.perf_counter()
                            if now - self.last_beep_time >= BEEP_COOLDOWN:
                                threading.Thread(target=self._play_alert, daemon=True).start()
                                self.last_beep_time = now
                        self._log("Detection paused after confirmed value.")
                    else:
                        self._log(f"Ignored a {CONFIRM_ATTEMPTS}-frame OCR false positive.")
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

    @staticmethod
    def _wave_color(phase, detected=False):
        """Blend red shades after detection; use the control colors otherwise."""
        palette = (("#FF3030", "#FF9090", "#A81818", "#FF3030") if detected else
                   (UI_PRIMARY, UI_ACCENT, UI_BUTTON, UI_PRIMARY))
        position = (phase % 1) * 3
        index = int(position)
        blend = position - index
        start, end = palette[index:index + 2]
        rgb = [round(int(start[i:i + 2], 16) * (1 - blend)
                     + int(end[i:i + 2], 16) * blend) for i in (1, 3, 5)]
        return "#" + "".join(f"{channel:02x}" for channel in rgb)

    def _render(self):
        """Fast, OCR-free UI repaint tick — just reflects whatever state the
        background OCR loop (or the spam loop) last wrote."""
        while not self._toggle_requests.empty():
            self._toggle_requests.get_nowait()
            self._toggle_enter_spam()
        if self._selection_requested.is_set():
            self._selection_requested.clear()
            self._start_selection()
        if self._autolocate_requested.is_set():
            self._autolocate_requested.clear()
            self._auto_locate()
        # Keep microphone operations outside the Windows keyboard hook.
        try:
            action = self._audio_requests.get_nowait()
        except queue.Empty:
            pass
        else:
            action()
            self._log(self.alert.status)
        color = self.COLOR_HIT if self.hit else self.COLOR
        if self._canvas:
            detected = self.hit
            if not detected:
                self._wave_phase = time.monotonic() / 8
            phase = self._wave_phase
            self._canvas.itemconfig(self._rect_id, outline=self._wave_color(phase, detected))
            for i, item in enumerate(self._wave_ids):
                self._canvas.itemconfig(item, fill=self._wave_color(phase - i / 64, detected))
            for corner, offset in {"nw": 0, "ne": 0.25, "se": 0.5, "sw": 0.75}.items():
                self._canvas.itemconfig(self._handle_ids[corner],
                                        fill=self._wave_color(phase - offset + 0.5, detected))
        if self._status_label:
            active = self.enter_on and not self.hit
            heading = "WATCHING" if active else ("DETECTED" if self.hit else "PAUSED")
            self._status_label.config(text=heading, fg=color if active or self.hit else UI_MUTED)
            detail = ("Press Start or F9 to watch the selected region."
                      if self.status_text == "paused" else self.status_text)
            if self.enter_spam_enabled and active:
                detail += "  Enter and click automation is active."
            self._detail_label.config(text=detail)
            button_text = ("Cancel start" if self._start_after is not None else
                           "Stop watching" if self.enter_on else "Start watching")
            self._start_button.config(text=f"{button_text}  ({self.enter_hotkey.upper()})",
                                      state="normal" if self._ocr_available and not self._selecting else "disabled")
            if self.beep_enabled:
                self._mute_button.deselect()
            else:
                self._mute_button.select()
            if self._discord_button is not None:
                if self.discord.enabled:
                    self._discord_button.select()
                else:
                    self._discord_button.deselect()
            self._record_button.config(text=("Save message" if self.alert.recording else "Record") + "  (F11)")
        activity_changed = False
        for _ in range(100):
            try:
                line = self._activity_pending.get_nowait()
            except queue.Empty:
                break
            self._activity_lines.append(line)
            activity_changed = True
        self._activity_lines = self._activity_lines[-6:]
        if self._activity_list and activity_changed:
            self._activity_list.config(state="normal")
            self._activity_list.delete("1.0", tk.END)
            for line in self._activity_lines:
                self._activity_list.insert(tk.END, line + "\n")
            self._activity_list.config(state="disabled")
            self._activity_list.yview_moveto(1)
        self.root.after(int(RENDER_INTERVAL * 1000), self._render)

    def _check_tesseract(self):
        try:
            pytesseract.get_tesseract_version()
        except Exception as error:
            self._ocr_available = False
            self.status_text = "Tesseract is unavailable - see activity"
            self._log(f"Tesseract unavailable: {error}")
        else:
            self._log("Tesseract ready.")

    def _check_autolocate(self):
        try:
            self._label_template_gray = np.array(Image.open(LABEL_TEMPLATE_PATH).convert("L"))
        except Exception as error:
            self._autolocate_available = False
            self._log(f"Auto-locate unavailable: {error}")
        else:
            self._log("Auto-locate ready (currently recognizes only the Combat Power reset dialog).")

    def _auto_locate(self):
        """F7: screenshot the whole desktop, find the 'Combat Power Change'
        label via multi-scale template matching, snap the box onto the AFTER
        number field(s), and move the real cursor onto the matching Reset
        button. Every panel shows this label — BEFORE and however many AFTER
        panels there are (1 for Reset x1, 3 for Reset x3) — and they always
        lay out left-to-right with BEFORE first, so the leftmost match is
        dropped and one box is sized to cover the rest: just the single
        number on x1, or all of them at once on x3. The detected panel count
        also picks which Reset button (x1 or x3) the cursor goes to — the
        button row is centered under the whole dialog rather than fixed
        relative to BEFORE, so its offset genuinely differs between the two
        dialog widths."""
        if not self._autolocate_available or self._selecting:
            return
        self._cancel_start()
        self._set_enter_on(False)
        self._unlock_mouse()
        self._log("Auto-locate: scanning the screen...")

        with mss.MSS() as sct:
            desktop = sct.monitors[0]
            shot = sct.grab(desktop)
        full_gray = cv2.cvtColor(
            np.array(Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")),
            cv2.COLOR_RGB2GRAY)
        tmpl_gray = self._label_template_gray
        th, tw = tmpl_gray.shape

        best_val, best_res, best_shape = -1.0, None, None
        for scale in AUTOLOCATE_SCALES:
            rt = cv2.resize(tmpl_gray, (max(1, round(tw * scale)), max(1, round(th * scale))))
            if rt.shape[0] > full_gray.shape[0] or rt.shape[1] > full_gray.shape[1]:
                continue
            res = cv2.matchTemplate(full_gray, rt, cv2.TM_CCOEFF_NORMED)
            _, maxval, _, _ = cv2.minMaxLoc(res)
            if maxval > best_val:
                best_val, best_res, best_shape = maxval, res, rt.shape

        if best_val < AUTOLOCATE_MIN_CONFIDENCE:
            self._log(f"Auto-locate: panel not found (best match {best_val:.2f}).")
            return

        # Pull out every distinct match at this scale (not just the global
        # best) by repeatedly taking the max and blanking a template-sized
        # area around it, so the BEFORE panel's near-identical label doesn't
        # get missed just because it scored a hair lower than the AFTER one.
        sh, sw = best_shape
        work = best_res.copy()
        peaks = []
        for _ in range(4):
            _, maxval, _, maxloc = cv2.minMaxLoc(work)
            if maxval < AUTOLOCATE_MIN_CONFIDENCE:
                break
            peaks.append(maxloc)
            x, y = maxloc
            x0, x1 = max(0, x - sw // 2), min(work.shape[1], x + sw // 2)
            y0, y1 = max(0, y - sh // 2), min(work.shape[0], y + sh // 2)
            work[y0:y1, x0:x1] = -1.0

        # Panels always lay out left-to-right with BEFORE first, whether it's
        # a Reset x1 (BEFORE + 1 AFTER = 2 matches) or Reset x3 (BEFORE + 3
        # AFTER = 4 matches) dialog, so the leftmost match is always BEFORE
        # and everything else is an AFTER panel that actually carries a
        # value. One box is sized to cover just the single AFTER number on
        # x1, or all of them at once (one wide box) on x3.
        peaks_sorted = sorted(peaks, key=lambda p: p[0])
        before_x, before_y = peaks_sorted[0]
        after_peaks = peaks_sorted[1:]
        if not after_peaks:
            self._log("Auto-locate: only found the BEFORE panel — AFTER panel(s) not detected.")
            return

        number_rects = []
        for (px, py) in after_peaks:
            ax = px + desktop["left"]
            ay = py + desktop["top"]
            nx = round(ax + LABEL_TO_NUMBER_DX_RATIO * sw)
            ny = round(ay + LABEL_TO_NUMBER_DY_RATIO * sh)
            nw = round(sw * NUMBER_WIDTH_RATIO)
            nh = round(sh * NUMBER_HEIGHT_RATIO)
            pad = round(sh * NUMBER_VERTICAL_PAD_RATIO)
            ny -= pad
            nh += pad * 2
            number_rects.append((nx, ny, nw, nh))

        left = min(r[0] for r in number_rects)
        top = min(r[1] for r in number_rects)
        right = max(r[0] + r[2] for r in number_rects)
        bottom = max(r[1] + r[3] for r in number_rects)

        self.region = [left, top, right - left, bottom - top]
        save_region(tuple(self.region))
        self._is_placeholder_region = False
        self._region_revision += 1
        self.hit = self._prev_hit = False
        self.last_value = None
        self.status_text = "paused"
        self._sync_geometry()
        self._log(f"Auto-locate: found {len(after_peaks)} AFTER panel(s) "
                  f"({best_val:.2f} confidence), moved box to {tuple(self.region)}.")

        if len(after_peaks) == 1:
            dx_ratio, dy_ratio = LABEL_TO_RESET_X1_DX_RATIO, LABEL_TO_RESET_X1_DY_RATIO
            w_ratio, h_ratio = RESET_X1_WIDTH_RATIO, RESET_X1_HEIGHT_RATIO
            button_name = "Reset x1"
        else:
            dx_ratio, dy_ratio = LABEL_TO_RESET_X3_DX_RATIO, LABEL_TO_RESET_X3_DY_RATIO
            w_ratio, h_ratio = RESET_X3_WIDTH_RATIO, RESET_X3_HEIGHT_RATIO
            button_name = "Reset x3"

        lx = before_x + desktop["left"]
        ly = before_y + desktop["top"]
        rx = round(lx + dx_ratio * sw)
        ry = round(ly + dy_ratio * sh)
        rw = round(sw * w_ratio)
        rh = round(sh * h_ratio)
        self._log(f"Auto-locate: moving cursor to {button_name}.")
        threading.Thread(target=smooth_move_to,
                          args=(rx + rw // 2, ry + rh // 2), daemon=True).start()

    def run(self):
        root = self._build_window()
        self._check_tesseract()
        self._check_autolocate()
        keyboard.add_hotkey("f8", self._selection_requested.set,
                            suppress=True, trigger_on_release=True)
        print("[hotkey] F8: draw a new region; Escape: cancel selection.", flush=True)
        keyboard.add_hotkey("f7", self._autolocate_requested.set,
                            suppress=True, trigger_on_release=True)
        print("[hotkey] F7: auto-locate the Combat Power Change panel and move the box + cursor there.", flush=True)
        keyboard.add_hotkey("f11", self._audio_requests.put, args=(self.alert.toggle,),
                            suppress=True, trigger_on_release=True)
        keyboard.add_hotkey("f12", self._audio_requests.put, args=(self.alert.reset,),
                            suppress=True, trigger_on_release=True)
        keyboard.add_hotkey(self.beep_hotkey, self._toggle_beep)
        keyboard.add_hotkey(self.enter_hotkey, self._toggle_requests.put, args=(True,),
                            suppress=True, trigger_on_release=True)
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

BORDER_COLOR = UI_ACCENT


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
    parser.add_argument("--no-enter-spam", action="store_true", help="disable automated Enter/click input; F9 still controls OCR")
    parser.add_argument("--enter-interval", type=float, default=ENTER_INTERVAL, help="seconds between spammed Enter presses (default: 0.16 = 160ms)")
    parser.add_argument("--click-interval", type=float, default=CLICK_INTERVAL, help="seconds between spammed left-clicks (default: 0.24 = 240ms)")
    parser.add_argument("--enter-hotkey", default=ENTER_HOTKEY, help="global Enter-spam on/off hotkey (default: f9)")
    parser.add_argument("--beep-hotkey", default=BEEP_HOTKEY, help="global beep mute/unmute hotkey (default: f10)")
    parser.add_argument("--elevate", action="store_true",
                         help="re-launch this script as Administrator (fixes F9 not responding "
                              "while an elevated game window has focus) — on by default, see --no-elevate")
    parser.add_argument("--no-elevate", action="store_true",
                         help="skip the automatic UAC elevation prompt on startup")
    parser.add_argument("--no-capture-check", action="store_true",
                         help="skip the startup re-check of the previous run's debug captures")
    parser.add_argument("--check-captures", action="store_true",
                         help="re-check the previous run's debug captures and print the result, no overlay")
    args = parser.parse_args()

    # Ahead of the elevation prompt on purpose: grading saved screenshots needs no
    # admin rights, and no saved region either, so neither should be demanded for it.
    if args.check_captures:
        def _progress(done, total):
            print(f"\r  Re-checking captures: {done}/{total}", end="", flush=True)
        summary = check_pending_captures(on_progress=_progress)
        if summary["checked"]:                      # only if a progress line was actually drawn
            print("\r" + " " * 40 + "\r", end="")
        print(format_check_summary(summary))
        sys.exit(0)

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

    # --reselect is an explicit ask, and --once has no GUI to fall back on, so
    # both still get the blocking full-screen selector. A first launch with
    # nothing saved yet does NOT — that used to drop straight into a
    # full-screen click-drag prompt before the control window ever appeared,
    # which read as the app randomly launching a screenshot tool. It now
    # starts with a harmless placeholder box instead, so the window opens
    # immediately and region selection stays an in-app action (F8/F7).
    if args.reselect or (region is None and args.once):
        print("  Opening region selector...")
        print()
        region = _open_selector()
    elif region is not None:
        print(f"  Last region: left={region[0]}  top={region[1]}  width={region[2]}  height={region[3]}")

    if args.once:
        value, _img = read_delta(region)
        print(f"detected: {value}" if value else "no '+<number>' found")
        sys.exit(0 if value else 1)

    is_placeholder = region is None
    if is_placeholder:
        region = default_region()
        print(f"  No saved region yet — starting with a placeholder box at {region}. "
              "Use 'Choose region' (F8) or 'Auto-locate' (F7) in the app to set the real one.")

    print(f"\n  Watching region={region} — drag the box to reposition, drag a corner to resize.")
    print("  Close the control window to quit.\n")
    app = OverlayApp(region, is_placeholder_region=is_placeholder, enter_spam=not args.no_enter_spam,
                     enter_hotkey=args.enter_hotkey,
                     enter_interval=args.enter_interval,
                     click_interval=args.click_interval,
                     beep_hotkey=args.beep_hotkey,
                     min_read_gap=args.interval)
    if not args.no_capture_check:
        # The exe has no console (console=False in the spec), so the printed summary
        # is invisible there — surface it in the app's own activity log instead.
        start_capture_check(on_done=lambda summary: app._log(format_check_summary(summary).strip()))
    app.run()
