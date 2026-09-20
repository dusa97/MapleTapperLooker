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
    import numpy as np
    from PIL import Image, ImageDraw, ImageTk
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

# Cubes: the Potential panel. Anchored on its "Potential" label (assets/reference/
# potential_label.png, cut from potential_example.png) the same way Flames anchors on
# "Combat Power Change"; the three stat lines sit at a fixed offset below it. Ratios are
# measured against the label's own size so they hold at any UI scale: label at (11,294)
# 50x15, the 3-line block at (90,306) 210x70 in the reference screenshot.
POTENTIAL_LABEL_PATH = Path(__file__).parent / "assets" / "reference" / "potential_label.png"
POTENTIAL_BLOCK_DX_RATIO = 79 / 50
POTENTIAL_BLOCK_DY_RATIO = 12 / 15
POTENTIAL_BLOCK_W_RATIO  = 250 / 50   # 210 fit "Skill Cooldowns -2 sec" with room; longer names exist
POTENTIAL_BLOCK_H_RATIO  = 70 / 15
CUBES_REGION_FILE = _BASE_DIR / "last_cubes_region.json"
CUBES_TARGET_FILE = _BASE_DIR / "last_cubes_target.json"
# What the Cubes tab lets you pick, and the words the OCR'd stat line must contain for
# each (the game's own spelling, from real captures under assets/reference/). Unit is
# what the line's value carries: '%' for everything except cooldowns (seconds).
POTENTIAL_STATS = (
    ("STR",             ("str",),               "%"),
    ("DEX",             ("dex",),               "%"),
    ("INT",             ("int",),               "%"),
    ("LUK",             ("luk",),               "%"),
    ("All Stats",       ("all", "stats"),       "%"),
    ("Attack Power",    ("attack", "power"),    "%"),
    ("Magic Attack",    ("magic", "att"),       "%"),
    ("Boss Damage",     ("boss", "damage"),     "%"),
    ("Ignore Defense",  ("ignore", "defense"),  "%"),
    ("Critical Damage", ("critical", "damage"), "%"),
    ("Item Drop Rate",  ("drop", "rate"),       "%"),   # 'item' is OCR-flaky ('Iterm' seen); 'drop rate' is enough
    ("Mesos Obtained",  ("mesos", "obtained"),  "%"),
    ("Skill Cooldowns", ("skill", "cooldowns"), "sec"),
)
# Attack Power / Magic ATT also come as FLAT lines ('Attack Power +32'); those are not the
# ones anyone cubes for and are ignored automatically because every option above is
# defined by unit - a flat value never counts toward a % goal, in total or combo mode.
# The tier squares beside each line are shown for information only; the target is
# purely the numbered total for the picked stat, whatever tier the lines are.
# After a cube, the loop does not read on a timer - it polls the box until the pixels
# differ from the pre-roll frame (the panel has redrawn with NEW lines), then OCRs once.
# A timer would read stale lines on a slow client and could stop on the previous roll.
CUBES_CHANGE_POLL   = 0.03   # seconds between cheap pixel-difference checks while waiting for the redraw
CUBES_CHANGE_FRAC   = 0.004  # fraction of pixels that must differ to count as "the panel changed"
CUBES_CHANGE_TIMEOUT = 3.0   # give up waiting after this long (no cubes left, dialog closed, ...)
CUBES_SETTLE_MAX     = 0.5   # after the first changed frame, wait up to this long for the redraw to finish
# One cube is exactly one fixed input sequence - left click, Enter, Enter - with a short gap
# between presses so the game registers each. No continuous spam: a fixed sequence can't
# land a press mid-read, so there is nothing to freeze and no race with the change detector.
CUBES_PRESS_GAP = 0.05       # seconds between the presses of one sequence (jittered +/-30%)
# The material row under the panel: the cube type in use sits on a cyan-highlighted slot
# (rgb ~(80,197,220)), 38x38 px, always 192 px below the "Potential" label, sliding
# sideways to whichever slot is selected. When that cube type runs out the game
# deselects it - no cyan slot anywhere, pill reads "Select a material item to use." -
# and that is the signal to stop instead of spamming through the game's prompts.
# Measured on four reference screenshots; ratios are against the label size.
CUBES_ROW_DY_RATIO  = 192 / 15      # label top -> highlight top
CUBES_ROW_H_RATIO   = 38 / 15       # highlight height (the row of slots)
CUBES_ROW_X0_RATIO  = -4 / 50       # search strip starts just left of the label's x
CUBES_ROW_W_RATIO   = 400 / 50      # ...and spans the whole slot row
CUBES_HIGHLIGHT_MIN_PX = 300        # cyan pixels needed to count as a highlighted slot (a full one is ~1400)
POTENTIAL_TARGET_HEIGHT = 210    # ~30px per stat line after upscaling (3 lines in the block)
# EACH stat line has its own small lettered square (R/E/U/L) at its left, coloured by that
# line's tier - an item can be L / U / U. Measured: 10x9 px at x 99..108, y 310..318 for
# the first line, i.e. block-relative x 9..19, y 4..13, lines 25 px apart. Colours, all
# measured on real captures under assets/reference/ (hue on PIL's 0-255 scale):
#   rare      (102,255,255) cyan    hue 127
#   epic      (187,119,255) purple  hue 191
#   unique    (255,204,  0) yellow  hue  34
#   legendary (204,255,  0) lime    hue  51
# Unique and legendary are only 17 hue units apart, so those two ranges are tight; the
# other gaps are wide.
POTENTIAL_ICON_X_FRAC = (9 / 210, 19 / 210)     # of block width
POTENTIAL_ICON_Y_FRAC = (4 / 70, 13 / 70)       # of block height, first line
POTENTIAL_LINE_STEP_FRAC = 25 / 70


class CubeProfile:
    """Everything the Cubes tab needs to know about ONE cube type's Potential
    panel: the label image to anchor on, where the three stat lines and
    their tier icons sit relative to it, and where the material row is.
    Glowing/Mystical/Hard/Solid cubes all share one panel layout (verified
    on 12 captures); Bright cubes get their own profile so their panel can
    be calibrated independently once a capture exists."""
    def __init__(self, name, label_path, block=(79 / 50, 12 / 15, 250 / 50, 70 / 15),
                 icon_x=(9 / 210, 19 / 210), icon_y=(4 / 70, 13 / 70), line_step=25 / 70,
                 row=(-4 / 50, 192 / 15, 400 / 50, 38 / 15), target_height=210, calibrated=True,
                 pick_label="only", cubes_left="highlight", count_box=None, commit_on_match=True):
        self.name = name
        self.label_path = label_path
        self.pick_label = pick_label          # "only": one label expected; "rightmost": AFTER card of a BEFORE/AFTER pair
        self.cubes_left = cubes_left          # "highlight": cyan slot in the material row; "count": OCR a remaining-count pill
        self.count_box = count_box            # (dx, dy, w, h) ratios of the label for the count pill, when cubes_left == "count"
        self.commit_on_match = commit_on_match  # False: the game shows the result before you commit, so a match means STOP, don't press
        self.block_dx, self.block_dy, self.block_w, self.block_h = block
        self.icon_x, self.icon_y, self.line_step = icon_x, icon_y, line_step
        self.row_x0, self.row_dy, self.row_w, self.row_h = row
        self.target_height = target_height
        self.calibrated = calibrated
        self._label_gray = None

    def label_gray(self):
        if self._label_gray is None:
            self._label_gray = np.array(Image.open(self.label_path).convert("L"))
        return self._label_gray

    def block_for(self, label_xy, label_wh):
        x, y = label_xy
        sh, sw = label_wh
        return (round(x + self.block_dx * sw), round(y + self.block_dy * sh),
                round(self.block_w * sw), round(self.block_h * sh))

    def row_for(self, label_box):
        lx, ly, sw, sh = label_box
        return (round(lx + self.row_x0 * sw), round(ly + self.row_dy * sh),
                round(self.row_w * sw), round(self.row_h * sh))


CUBE_PROFILES = {
    "glowing": CubeProfile("Glowing", POTENTIAL_LABEL_PATH),
    # Bright cubes use the game's Reset dialog: BEFORE and AFTER cards side by side, the new
    # roll shown BEFORE you commit, "Reset x1" to roll again (which makes AFTER the new
    # BEFORE). Measured on assets/reference/bright_example.png: Flames' own "Combat Power
    # Change" template finds both cards at 1.000; the rightmost is AFTER, and its three lines
    # sit ~100 px ABOVE that label (block 202x70 at (288,312) for a 146x10 label at
    # (324,413)), icons at the same per-line positions and tier colours as the Potential
    # panel. Cubes left is the "Remaining" pill bottom-left, 258 px left / 213 px below the
    # label. A match means stop and leave the dialog for the user - pressing would reset it.
    "bright": CubeProfile("Bright", LABEL_TEMPLATE_PATH,
                          block=(-36 / 146, -101 / 10, 202 / 146, 70 / 10),
                          icon_x=(9 / 202, 19 / 202), icon_y=(6 / 70, 16 / 70), line_step=24 / 70,
                          pick_label="rightmost", cubes_left="count",
                          count_box=(-258 / 146, 213 / 10, 58 / 146, 20 / 10),
                          commit_on_match=False),
}
CUBE_TYPE_DEFAULT = "glowing"


def active_cube_profile():
    """The profile for the cube type chosen in the Cubes tab (persisted in
    MODE_FILE); Glowing until told otherwise."""
    try:
        key = json.loads(MODE_FILE.read_text(encoding="utf-8")).get("cube_type")
    except Exception:
        key = None
    return CUBE_PROFILES.get(key, CUBE_PROFILES[CUBE_TYPE_DEFAULT])
POTENTIAL_TIER_HUES = (("unique", 20, 42), ("legendary", 43, 75), ("rare", 100, 160), ("epic", 170, 220))
POTENTIAL_TIERS = ("rare", "epic", "unique", "legendary")   # ascending
POTENTIAL_TIER_COLOURS = {"rare": "#66FFFF", "epic": "#BB77FF", "unique": "#FFCC00", "legendary": "#CCFF00"}
# Words as well as digits here, so Tesseract keeps its full alphabet; the stat line text is
# bright and desaturated on a dark card, so the Flames colour mask isolates it unchanged.
POTENTIAL_OCR_CONFIG = r'--psm 6 -c tessedit_char_whitelist=+-0123456789%ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz: '
POTENTIAL_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z ]*?):?\s*([+-]\d+(?:%|[A-Za-z]+)?)\d?$")  # trailing \d?: 'INT:+10%6' seen once
POTENTIAL_VALUE_RE = re.compile(r"([+-])(\d+) ?(%|[A-Za-z]+)?")  # sign, number, unit ('' / '%' / 'sec')
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


_cv2 = None


def _load_cv2():
    """cv2 is imported here, on demand, instead of at the top with everything
    else - it is only used by F7 auto-locate (matchTemplate over the whole
    desktop at 31 scales, which numpy can't do fast enough: an FFT version
    measured 10x slower). Measured inside the frozen exe, `import cv2` alone
    cost 1.27 s of a 2.65 s launch: Windows has to map and security-check the
    86 MB cv2.pyd that was just written to %TEMP%. Deferring it moves that
    cost from every launch to the first F7 press, where it isn't noticed.
    Called once at startup on a background thread so even that is warm by
    the time anyone presses F7."""
    global _cv2
    if _cv2 is None:
        import cv2
        _cv2 = cv2
    return _cv2


def locate_label(full_gray, tmpl_gray, max_peaks=4):
    """Multi-scale TM_CCOEFF_NORMED search for *tmpl_gray* in *full_gray*.
    Returns (best_score, peaks, (h, w)) where peaks are the (x, y) top-left
    corners of every distinct match at the best scale, strongest first, and
    (h, w) is the template size at that scale. Shared by Flames (Combat
    Power Change label) and Cubes (Potential label)."""
    cv2 = _load_cv2()
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
    if best_res is None or best_val < AUTOLOCATE_MIN_CONFIDENCE:
        return best_val, [], best_shape
    sh, sw = best_shape
    work = best_res.copy()
    peaks = []
    for _ in range(max_peaks):
        _, maxval, _, maxloc = cv2.minMaxLoc(work)
        if maxval < AUTOLOCATE_MIN_CONFIDENCE:
            break
        peaks.append(maxloc)
        x, y = maxloc
        work[max(0, y - sh // 2):min(work.shape[0], y + sh // 2),
             max(0, x - sw // 2):min(work.shape[1], x + sw // 2)] = -1.0
    return best_val, peaks, best_shape


def read_potential_tiers(img, profile=None):
    """Tier of each of the three stat lines, from the colour of the lettered
    icon beside it: a list of 3 entries, each 'rare' / 'epic' / 'unique' /
    'legendary' or None (no saturated icon there - line absent, panel not
    showing, box misplaced). The item's own rank is the first line's tier."""
    profile = profile or active_cube_profile()
    w, h = img.size
    hsv_all = np.array(img.convert("HSV")).astype(int)
    x0, x1 = (round(w * f) for f in profile.icon_x)
    tiers = []
    for i in range(3):
        y0 = round(h * (profile.icon_y[0] + i * profile.line_step))
        y1 = round(h * (profile.icon_y[1] + i * profile.line_step))
        hsv = hsv_all[y0:max(y0 + 1, y1), x0:max(x0 + 1, x1)]
        coloured = (hsv[..., 1] > 100) & (hsv[..., 2] > 120)
        tier = None
        if coloured.sum() >= 6:
            hue = int(np.median(hsv[..., 0][coloured]))
            tier = next((name for name, lo, hi in POTENTIAL_TIER_HUES if lo <= hue <= hi), None)
        tiers.append(tier)
    return tiers


def read_potential_lines(img, profile=None):
    """OCR the Potential card's stat lines out of a raw crop. Returns a list
    of (stat, value) like [("Max HP", "+120"), ("Max MP", "+60"), ("DEF",
    "+60")]; lines Tesseract garbles come back as (raw_text, None) rather
    than being dropped, so the UI can show what it saw."""
    # Scale so each stat line lands near ~30px tall - the block is three lines,
    # so target 3x that for the whole crop. Tesseract is fussy about this font's
    # "M" at in-between scales (2.5x/3.5x/4x misread it as "W"/"hl"), while 2x
    # and 3x read cleanly, so aim for the middle of the good range.
    profile = profile or active_cube_profile()
    scale = max(OCR_SCALE_MIN, min(profile.target_height / img.height, OCR_SCALE_MAX))
    big = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    hsv = np.array(big.convert("HSV"))
    ink = (hsv[..., 2].astype(int) > OCR_MASK_VALUE_MIN) & (hsv[..., 1].astype(int) < OCR_MASK_SATURATION_MAX)
    proc = Image.fromarray(np.where(ink, 0, 255).astype("uint8"), mode="L")
    text = pytesseract.image_to_string(proc, config=POTENTIAL_OCR_CONFIG)
    out = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = POTENTIAL_LINE_RE.match(raw.replace(" ", ""))
        if m:
            # Tesseract drops the spaces ("MaxHP+120", "-2sec"); put them back
            stat = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", m.group(1))
            value = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", m.group(2))
            out.append((stat, value))
        else:
            out.append((raw, None))
    return out


POTENTIAL_TARGET_RE = re.compile(r"^\s*(?:([+-]?\d+)\s*(%?)\s*([A-Za-z][A-Za-z ]*?)|([A-Za-z][A-Za-z ]*?)\s*\+?\s*(\d+)\s*(%?))\s*$")
POTENTIAL_TIER_WORD_RE = re.compile(r"\b(rare|epic|unique|legendary)\b", re.I)


def parse_potential_target(text):
    """Turn what the user typed into (stat_words, minimum, wants_percent,
    tier). Accepts "30% str", "str 30%", "str +30", "120 max hp", "max hp
    120", with an optional tier word anywhere ("unique 30% str"), or a
    tier on its own ("legendary" = stop as soon as the item is legendary,
    whatever the lines say). stat_words is a tuple of lowercase words that
    must ALL appear in the stat name on the line; tier is a lowercase tier
    name or None. Returns None if it can't be understood."""
    text = text or ""
    tier = None
    tm = POTENTIAL_TIER_WORD_RE.search(text)
    if tm:
        tier = tm.group(1).lower()
        text = text[:tm.start()] + text[tm.end():]
    if tier and not text.strip():
        return (), 0, False, tier
    m = POTENTIAL_TARGET_RE.match(text)
    if not m:
        return None
    if m.group(1) is not None:
        number, pct, stat = m.group(1), m.group(2), m.group(3)
    else:
        stat, number, pct = m.group(4), m.group(5), m.group(6)
    words = tuple(w for w in re.split(r"\s+", stat.strip().lower()) if w)
    if not words:
        return None
    return words, abs(int(number)), pct == "%", tier


def tier_satisfies(tier, wanted):
    """Is the read *tier* at least the *wanted* one? None wanted = no
    requirement; None read = unknown, never satisfies a requirement."""
    if wanted is None:
        return True
    if tier is None:
        return False
    return POTENTIAL_TIERS.index(tier) >= POTENTIAL_TIERS.index(wanted)


POTENTIAL_BASE_STATS = ("str", "dex", "int", "luk")


ALL_STATS_COUNTS = True   # Cubes tab checkbox: does an 'All Stats' line count toward STR/DEX/INT/LUK?


def potential_total_for(lines, tiers, target):
    """Sum of the lines that count toward *target* on this item. A line
    counts if its stat contains the target words - or it is 'All Stats'
    and the target is one of STR/DEX/INT/LUK, since All Stats adds to each
    of them - AND its unit matches AND (if the target names a tier) the
    line itself is at least that tier. So 12% LUK + 9% All Stats + 9% LUK
    is 30% LUK, and a rare 9% LUK line does not help a 'Unique+' target.
    Magnitude is summed so cooldowns (-2 sec, -1 sec) total 3."""
    words, _minimum, wants_pct, wanted_tier = target
    if not words:
        return 0
    counts_all_stats = ALL_STATS_COUNTS and len(words) == 1 and words[0] in POTENTIAL_BASE_STATS
    total = 0
    for line, tier in zip(lines, tiers):
        stat, value = line
        if value is None or not tier_satisfies(tier, wanted_tier):
            continue
        haystack = re.sub(r"\s+", " ", stat.lower())
        is_match = all(w in haystack for w in words)
        is_all_stats = counts_all_stats and "all" in haystack and "stat" in haystack
        if not (is_match or is_all_stats):
            continue
        m = POTENTIAL_VALUE_RE.fullmatch(value)
        if not m or ((m.group(3) or "") == "%") != wants_pct:
            continue
        total += int(m.group(2))
    return total


def _stat_words(name):
    return next((w for n, w, _u in POTENTIAL_STATS if n == name), None)


def _stat_unit(name):
    return next((u for n, _w, u in POTENTIAL_STATS if n == name), "%")


def _line_is_stat(line, words, unit="%"):
    """Does this OCR'd line belong to the stat with these match words, in
    the right unit? A flat 'Attack Power +32' is not the % stat of the
    same name. All Stats counts as any of STR/DEX/INT/LUK."""
    stat, value = line
    if value is None:
        return False
    m = POTENTIAL_VALUE_RE.fullmatch(value)
    if not m or (m.group(3) or "") != unit:
        return False
    haystack = re.sub(r"\s+", " ", stat.lower())
    if all(w in haystack for w in words):
        return True
    return (ALL_STATS_COUNTS and len(words) == 1 and words[0] in POTENTIAL_BASE_STATS
            and "all" in haystack and "stat" in haystack)


class TotalGoal:
    """Stop when the item's summed value for one stat reaches a minimum
    (the original mode). Carries the (words, minimum, wants_pct, None)
    tuple the matcher functions take."""
    def __init__(self, name, minimum, unit):
        self.name, self.minimum, self.unit = name, minimum, unit
        self.tuple = (_stat_words(name), minimum, unit == "%", None)

    def describe(self):
        return f"{self.name} >= {self.minimum}{self.unit if self.unit == '%' else ' ' + self.unit} (total)"

    def check(self, lines, tiers):
        total = potential_total_for(lines, tiers, self.tuple)
        return f"{self.name} {total}{self.unit if self.unit == '%' else ' ' + self.unit} total" if total >= self.minimum else None

    def progress(self, lines, tiers):
        total = potential_total_for(lines, tiers, self.tuple)
        u = self.unit if self.unit == "%" else " " + self.unit
        return f"{self.name.upper()} counted: {total}{u}  (need {self.minimum}{u})"


class ComboGoal:
    """Stop when at least *count* of the three lines belong to the chosen
    set of stats, in any mix - 'LUK' alone means LUK/All Stats lines;
    'Attack Power + Boss Damage' means lines that are either of those."""
    def __init__(self, names, count=3):
        self.names = list(names)
        self.count = max(1, min(3, int(count)))
        self.words = [(_stat_words(n), _stat_unit(n)) for n in self.names]

    def describe(self):
        return f"{self.count} of 3 lines are " + " / ".join(self.names)

    def _matching(self, lines):
        return [any(_line_is_stat(line, w, u) for w, u in self.words) for line in lines]

    def check(self, lines, tiers):
        n = sum(self._matching(lines)[:3])
        return f"{n} of 3 lines match" if n >= self.count else None

    def progress(self, lines, tiers):
        n = sum(self._matching(lines)[:3])
        return f"{n} of 3 lines are {' / '.join(self.names)}  (need {self.count})"


class PerStatGoal:
    """Stop when, for EVERY chosen stat, at least its own count of lines are
    that stat - '2 x Critical Damage + 1 x STR' needs two crit lines and a
    STR line on the same item. Each line is spent on one stat only, so
    the requirements can't overlap (an All Stats line satisfies whichever
    base stat still needs one)."""
    def __init__(self, needs):                 # {stat name: count}, counts >= 1
        self.needs = {n: max(1, min(3, int(c))) for n, c in needs.items() if int(c) > 0}
        self.words = {n: (_stat_words(n), _stat_unit(n)) for n in self.needs}

    def describe(self):
        return " + ".join(f"{c} x {n}" for n, c in self.needs.items())

    def _have(self, lines):
        """How many lines each stat gets, assigning each line once - exact
        matches first, then All Stats lines to whichever base stat is short."""
        have = {n: 0 for n in self.needs}
        free = list(lines[:3])
        for n, (w, u) in self.words.items():                 # exact stat name
            for line in list(free):
                if have[n] < self.needs[n] and _line_is_stat(line, w, u) and not _is_all_stats(line):
                    have[n] += 1
                    free.remove(line)
        for n, (w, u) in self.words.items():                 # All Stats fills remaining base-stat needs
            for line in list(free):
                if have[n] < self.needs[n] and _line_is_stat(line, w, u):
                    have[n] += 1
                    free.remove(line)
        return have

    def check(self, lines, tiers):
        have = self._have(lines)
        return "all requirements met" if all(have[n] >= c for n, c in self.needs.items()) else None

    def progress(self, lines, tiers):
        have = self._have(lines)
        return "  ".join(f"{n}: {have[n]}/{c}" for n, c in self.needs.items())


def _is_all_stats(line):
    h = re.sub(r"\s+", " ", (line[0] or "").lower())
    return "all" in h and "stat" in h


class LegendaryOnly:
    """Wrap a goal so it only sees legendary lines, and additionally
    requires all three lines to be legendary. Ticking 'all legendary' on
    a goal kind wraps that goal; the others are unaffected."""
    def __init__(self, goal):
        self.goal = goal

    def describe(self):
        return self.goal.describe() + " (all 3 lines legendary)"

    @staticmethod
    def _legendary(lines, tiers):
        return [line if tier == "legendary" else (line[0], None) for line, tier in zip(lines, tiers)]

    def check(self, lines, tiers):
        if len(tiers) < 3 or any(t != "legendary" for t in tiers[:3]):
            return None
        return self.goal.check(self._legendary(lines, tiers), tiers)

    def progress(self, lines, tiers):
        n = sum(1 for t in tiers[:3] if t == "legendary")
        return self.goal.progress(self._legendary(lines, tiers), tiers) + f"  [{n}/3 legendary]"


class AnyOfGoal:
    """The roll stops as soon as ANY enabled goal is satisfied. This is how
    the three goal kinds combine when more than one is ticked."""
    def __init__(self, goals):
        self.goals = list(goals)

    def describe(self):
        return "  OR  ".join(g.describe() for g in self.goals)

    def check(self, lines, tiers):
        for g in self.goals:
            found = g.check(lines, tiers)
            if found:
                return found
        return None

    def progress(self, lines, tiers):
        return "\n\u2192 ".join(g.progress(lines, tiers) for g in self.goals)


def line_matches_target(line, target):
    """Does one OCR'd (stat, value) line satisfy a parsed target? The stat
    must contain every target word ("str" matches "STR" and "str" lines
    only - "All Stats" does not contain "str", so it is not a match), the
    value must be a positive number at least the minimum, and % must agree
    (a flat "+30" does not satisfy "30%", and vice versa)."""
    stat, value = line
    if value is None:
        return False
    words, minimum, wants_pct, _tier = target
    if not words:
        return False                  # tier-only target: lines never satisfy it, the tier does
    haystack = re.sub(r"\s+", " ", stat.lower())
    if not all(w in haystack for w in words):
        return False
    m = POTENTIAL_VALUE_RE.fullmatch(value)
    if not m:
        return False
    # Magnitude, so "Skill Cooldowns -2 sec" satisfies a target of "2 skill
    # cooldowns" - for that stat more negative is better and the sign is the
    # game's, not the user's.
    return int(m.group(2)) >= minimum and ((m.group(3) or "") == "%") == wants_pct


def total_potential_lines(lines):
    """Sum the read lines by stat, keeping % and flat values apart, in first-
    seen order: [("STR","+12%"),("STR","+9%"),("LUK","+9%")] ->
    [("STR","+21%"),("LUK","+9%")]. Stat names are compared case-
    insensitively with whitespace collapsed, so "Max HP" and "MAX HP" add."""
    totals = {}
    for stat, value in lines:
        m = POTENTIAL_VALUE_RE.fullmatch(value or "")
        if not m:
            continue
        unit = m.group(3) or ""
        key = (re.sub(r"\s+", " ", stat.strip().upper()), unit)
        totals[key] = totals.get(key, 0) + int(m.group(1) + m.group(2))
    return [(stat, f"{n:+d}{unit if unit == '%' else (' ' + unit if unit else '')}")
            for (stat, unit), n in totals.items()]


def _read_app_version() -> str:
    """Release tag baked in by the release workflow (it overwrites
    assets/version.txt with the tag before building); 'dev' when running
    from source or a local build. Shown in the window title so a screenshot
    or a bug report says which build it came from without anyone asking."""
    try:
        return (Path(__file__).parent / "assets" / "version.txt").read_text(encoding="utf-8").strip() or "dev"
    except Exception:
        return "dev"


APP_VERSION = _read_app_version()
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
    """Area (box) resampling is for shrinking and gives corrupted results
    when used to enlarge a small image — confirmed directly: a minus sign's
    natural ~4px-tall crop, stretched to the 40px template height with area
    resampling, produced a solid corrupted blob instead of a stretched bar,
    which then wrongly out-scored real digits during classification. Digits
    are close enough to the target size that this mostly didn't show up for
    them, but it must still be handled correctly in general — box only when
    actually shrinking, bilinear when enlarging.

    Done with PIL rather than cv2 so this hot path (every glyph of every
    read) doesn't need cv2 loaded at all — see _load_cv2 for why that
    matters. PIL's BOX/BILINEAR reproduce cv2's INTER_AREA/INTER_LINEAR
    closely enough that classification agreed on 496/496 real glyphs."""
    h, w = glyph_arr.shape[:2]
    dst_w, dst_h = size
    method = Image.BOX if (h >= dst_h and w >= dst_w) else Image.BILINEAR
    return np.asarray(Image.fromarray(glyph_arr).resize(size, method))


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

_LOGO_CACHE = {}


def logo_photo(size, master):
    """ImageTk.PhotoImage of assets/logo.png at *size* px. The decode +
    LANCZOS resize is done once per size and cached (it was 58 ms of every
    Flames start, i.e. of every tab switch); only the cheap Tk handle is
    made per window."""
    if size not in _LOGO_CACHE:
        with Image.open(Path(__file__).parent / "assets" / "logo.png") as image:
            _LOGO_CACHE[size] = image.resize((size, size), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(_LOGO_CACHE[size], master=master)


def open_discord_settings(parent, discord, log, on_change=None):
    if not discord.supported:
        return
    current = discord.settings
    dialog = tk.Toplevel(parent)
    dialog.title("Discord settings")
    dialog.configure(bg=UI_BG)
    dialog.resizable(False, False)
    dialog.transient(parent)
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
            discord.save_settings(webhook.get(), user_id.get())
        except ValueError as exc:
            error.config(text=str(exc))
            return
        except OSError:
            error.config(text="Could not save settings. Previous settings were kept.")
            return
        log("Discord settings saved.")
        dialog.destroy()

    def clear():
        try:
            discord.clear()
        except OSError:
            error.config(text="Could not clear settings.")
            return
        if on_change is not None:
            on_change()
        log("Discord settings cleared and disabled.")
        dialog.destroy()

    def test_alert():
        if discord.settings is None:
            error.config(text="Save valid settings before sending a test alert.")
            return
        if not discord.schedule("", test=True):
            error.config(text="A Discord alert is already pending.")

    OverlayApp._button(buttons, "Save", save, UI_PRIMARY, UI_BG).pack(side="left")
    OverlayApp._button(buttons, "Cancel", dialog.destroy, UI_BUTTON, UI_TEXT).pack(side="left", padx=8)
    OverlayApp._button(buttons, "Clear settings", clear, UI_BUTTON, UI_TEXT).pack(side="left")
    OverlayApp._button(buttons, "Send test alert", test_alert, UI_BUTTON, UI_TEXT).pack(side="left", padx=(8, 0))
    webhook.focus_set()


def build_header(parent, logo_image):
    """The app's header - logo, name, version, subtitle, accent rule. One
    header for the whole app: in the tabbed shell it sits above the tabs
    (run_app), and only the controls under it change per tab; when Flames
    runs standalone (host=None) it draws the same header itself."""
    header = tk.Frame(parent, bg=UI_BG)
    header.pack(fill="x", pady=(0, 14))
    tk.Label(header, image=logo_image, bg=UI_BG).pack(pady=(0, 10))
    heading = tk.Frame(header, bg=UI_BG)
    heading.pack()
    tk.Label(heading, text="MAPLE / TAPPER LOOKER", fg=UI_TEXT, bg=UI_BG,
             font=("Segoe UI", 16, "bold"), anchor="center").pack(fill="x")
    tk.Label(heading, text=f"version {APP_VERSION}", fg=UI_ACCENT, bg=UI_BG,
             font=("Segoe UI", 10, "bold"), anchor="center").pack(fill="x", pady=(2, 0))
    tk.Label(heading, text="OCR and input automation control", fg=UI_MUTED, bg=UI_BG,
             font=("Segoe UI", 10), anchor="center").pack(fill="x", pady=(4, 0))
    accent = tk.Frame(parent, bg=UI_BORDER, height=2)
    accent.pack(pady=(0, 18))
    tk.Frame(accent, bg=UI_PRIMARY, width=80, height=2).pack(side="left")
    tk.Frame(accent, bg=UI_ACCENT, width=40, height=2).pack(side="left")
    return header


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
                 discord=None, discord_allowed=None,
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
        # The notifier is normally shared and owned by the tabbed shell (run_app),
        # which also owns the per-tab on/off switches; standalone, make our own.
        self._owns_discord = discord is None
        self.discord = discord if discord is not None else DiscordNotifier(log=self._log)
        self._discord_allowed = discord_allowed if discord_allowed is not None else (lambda: True)
        self._discord_button = None
        self._discord_settings_button = None
        self._mute_var = None
        self._discord_var = None

    def _build_window(self, host=None):
        """Build the normal taskbar window plus the capture-safe screen overlay.
        With *host* (a frame inside the tabbed shell, see run_app) the Flames
        UI is built into that frame and the shell owns the window - title,
        icon, size, close button - so none of that is touched here."""
        if host is None:
            root = tk.Tk()
            root.title(f"Maple Tapper Looker {APP_VERSION}")
        else:
            root = host.winfo_toplevel()
        if host is None:
            self._logo_image = logo_photo(144, root)
            self._icon_image = logo_photo(256, root)
            root.iconphoto(True, self._icon_image)
            root.resizable(False, False)
            root.configure(bg=UI_BG)
            root.geometry("540x610")
            root.minsize(500, 560)
            root.protocol("WM_DELETE_WINDOW", self._quit)
            root.option_add("*Font", ("Segoe UI", 10))

        outer = tk.Frame(host if host is not None else root, bg=UI_BG, padx=24,
                         pady=20 if host is None else 0)
        outer.pack(side="left", fill="both", expand=True)
        if host is None:
            build_header(outer, self._logo_image)

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

        if self._owns_discord:
            # Standalone only: in the shell the Discord controls sit above the tabs.
            discord = self._card(outer)
            discord.pack(fill="x", pady=(14, 0))
            tk.Label(discord, text="Discord alerts", fg=UI_TEXT, bg=UI_SURFACE,
                     font=("Segoe UI", 12, "bold"), anchor="center").pack(fill="x", padx=16, pady=(14, 3))
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
            self._discord_settings_button = self._button(
                discord_buttons, "Discord settings",
                self._open_discord_settings,
                UI_BUTTON, UI_TEXT)
            self._discord_settings_button.pack(side="left", padx=(8, 0))
            if not self.discord.supported:
                self._log("Discord alerts need Windows DPAPI and are unavailable here.")
        else:
            self._discord_var = None

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
        self.root, self.overlay, self._canvas, self._outer = root, overlay, canvas, outer
        self._sync_geometry()
        if host is None:
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
        self.stop()
        self.root.destroy()

    def stop(self):
        """Release everything this mode holds - hotkeys, threads, mouse lock,
        overlay, its widgets - but leave the Tk root alone, so the tabbed
        shell (run_app) can keep the window and start the other mode in it.
        Idempotent: the shell calls it on every tab switch and on close."""
        if not self._running:
            return
        print("\nStopped.")
        self._running = False
        if self._owns_discord:
            self.discord.close()
        target = getattr(self, "_activation_target", None)
        if target is not None and hasattr(target, "close"):
            target.close()
        self._unlock_mouse()
        self.alert.close()
        hotkeys = ["f7", "f8", "f11", "f12", self.beep_hotkey, self.enter_hotkey]
        for hk in hotkeys:
            try:
                keyboard.remove_hotkey(hk)
            except (KeyError, ValueError):
                pass
        self._cancel_start()
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
        outer = getattr(self, "_outer", None)
        if outer is not None and outer.winfo_exists():
            outer.destroy()

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
                self.region, own_hwnds, report=self._log if self._discord_on() else None)
            if self._discord_on() and self._activation_target is None:
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

    def _discord_on(self):
        return self.discord.enabled and self._discord_allowed()

    def _open_discord_settings(self):
        """Standalone Flames: the shared dialog, parented to our own root."""
        def cleared():
            if self._discord_var is not None:
                self._discord_var.set(False)
        open_discord_settings(self.root, self.discord, self._log, on_change=cleared)

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
                        if self._discord_on():
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
        if not self._running:
            return                      # stop() ran; widgets are gone, don't reschedule
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
        """Spawns `tesseract --version` (25-120 ms) - off the UI thread, so a
        tab switch into Flames doesn't stall on it. _ocr_available starts
        True and only ever flips to False here, and _log is queue-based, so
        nothing about this needs the main thread."""
        def probe():
            try:
                pytesseract.get_tesseract_version()
            except Exception as error:
                self._ocr_available = False
                self.status_text = "Tesseract is unavailable - see activity"
                self._log(f"Tesseract unavailable: {error}")
            else:
                self._log("Tesseract ready.")
        threading.Thread(target=probe, daemon=True, name="tesseract-probe").start()

    def _check_autolocate(self):
        try:
            self._label_template_gray = np.array(Image.open(LABEL_TEMPLATE_PATH).convert("L"))
        except Exception as error:
            self._autolocate_available = False
            self._log(f"Auto-locate unavailable: {error}")
            return
        self._log("Auto-locate ready (currently recognizes only the Combat Power reset dialog).")
        # Warm cv2 off the main thread — see _load_cv2. Must NOT run inline here:
        # this is called during startup, and doing the 1.3 s import on the main
        # thread would just move the delay to before the window instead of
        # removing it. A failure only surfaces on F7, where _auto_locate reports it.
        threading.Thread(target=_load_cv2, daemon=True, name="cv2-warmup").start()

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
        full_gray = np.array(Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").convert("L"))
        try:
            # Every distinct match at the best scale, not just the global best, so
            # the BEFORE panel's near-identical label isn't missed for scoring a
            # hair under the AFTER one.
            best_val, peaks, best_shape = locate_label(full_gray, self._label_template_gray)
        except Exception as error:
            self._log(f"Auto-locate unavailable: {error}")
            return
        if not peaks:
            self._log(f"Auto-locate: panel not found (best match {best_val:.2f}).")
            return
        sh, sw = best_shape

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

    def run(self, host=None):
        root = self._build_window(host)
        self._check_tesseract()
        self._check_autolocate()

        def register_hotkeys():
            # keyboard.add_hotkey re-installs its global Windows hook on the first
            # call after a remove - measured up to 225 ms - which is why this runs
            # on a worker thread: every callback here only pushes onto a queue or
            # flips a flag (never touches Tk), so nothing about it needs the UI
            # thread, and a tab switch into Flames stays instant.
            keyboard.add_hotkey("f8", self._selection_requested.set,
                                suppress=True, trigger_on_release=True)
            keyboard.add_hotkey("f7", self._autolocate_requested.set,
                                suppress=True, trigger_on_release=True)
            keyboard.add_hotkey("f11", self._audio_requests.put, args=(self.alert.toggle,),
                                suppress=True, trigger_on_release=True)
            keyboard.add_hotkey("f12", self._audio_requests.put, args=(self.alert.reset,),
                                suppress=True, trigger_on_release=True)
            keyboard.add_hotkey(self.beep_hotkey, self._toggle_beep)
            keyboard.add_hotkey(self.enter_hotkey, self._toggle_requests.put, args=(True,),
                                suppress=True, trigger_on_release=True)
        threading.Thread(target=register_hotkeys, daemon=True, name="hotkeys").start()
        print("[hotkey] F8: draw a new region; Escape: cancel selection.", flush=True)
        print("[hotkey] F7: auto-locate the Combat Power Change panel and move the box + cursor there.", flush=True)
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
        if host is not None:
            return                      # the shell owns the mainloop
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



MODE_FILE = _BASE_DIR / "last_mode.json"


def load_discord_modes():
    """Which tabs may post Discord alerts: {'flames': bool, 'cubes': bool}.
    Lives in MODE_FILE next to the last-tab choice; the webhook itself
    stays in the DPAPI-protected DiscordSettingsStore."""
    try:
        data = json.loads(MODE_FILE.read_text(encoding="utf-8")).get("discord", {})
        return {"flames": bool(data.get("flames", True)), "cubes": bool(data.get("cubes", True))}
    except Exception:
        return {"flames": True, "cubes": True}


def save_discord_modes(modes):
    try:
        data = json.loads(MODE_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["discord"] = {"flames": bool(modes.get("flames")), "cubes": bool(modes.get("cubes"))}
    try:
        MODE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def load_mode():
    try:
        mode = json.loads(MODE_FILE.read_text(encoding="utf-8")).get("mode")
        return mode if mode in ("flames", "cubes") else None
    except Exception:
        return None


def save_mode(mode):
    try:
        data = json.loads(MODE_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["mode"] = mode
    try:
        MODE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def run_flames(args, host=None, discord=None, discord_modes=None):
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
                         discord=discord, discord_allowed=(lambda: discord_modes()["flames"]) if discord_modes else None,
                         enter_hotkey=args.enter_hotkey,
                         enter_interval=args.enter_interval,
                         click_interval=args.click_interval,
                         beep_hotkey=args.beep_hotkey,
                         min_read_gap=args.interval)
        if not args.no_capture_check:
            # The exe has no console (console=False in the spec), so the printed summary
            # is invisible there — surface it in the app's own activity log instead.
            start_capture_check(on_done=lambda summary: app._log(format_check_summary(summary).strip()))
        app.run(host)
        return app


class MouseLock:
    """ClipCursor pin, same technique as OverlayApp._lock_mouse: Windows drops
    the clip on every focus change, so the loop re-applies it on a timer."""
    def __init__(self):
        self._rect = None

    def lock(self):
        if platform.system() != "Windows":
            return
        try:
            import ctypes
            from ctypes import wintypes
            pt = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            self._rect = wintypes.RECT(pt.x, pt.y, pt.x + 1, pt.y + 1)
            ctypes.windll.user32.ClipCursor(ctypes.byref(self._rect))
        except Exception:
            pass

    def reassert(self):
        if self._rect is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.ClipCursor(ctypes.byref(self._rect))
        except Exception:
            pass

    def unlock(self):
        if platform.system() != "Windows":
            return
        try:
            import ctypes
            ctypes.windll.user32.ClipCursor(None)
        except Exception:
            pass
        self._rect = None


class CubesApp:
    """The Cubes tab: locate the Potential panel (F7) or draw a box on its
    three stat lines (F8), then read them (F9 or the button). No spam, no
    detection loop - a read happens only when asked. Same stop() contract
    as OverlayApp so the tabbed shell treats both modes alike."""
    COLOR = UI_ACCENT
    BORDER = 4          # visible frame width; the frame sits OUTSIDE the capture region
    HANDLE = 10         # corner squares you drag to resize
    MIN_SIZE = 40

    def __init__(self, host, discord=None, discord_modes=None):
        self.host = host
        self.root = host.winfo_toplevel()
        self.discord = discord
        self._discord_modes = discord_modes or (lambda: {"cubes": False})
        self._activation_target = None
        self.region = self._load_region()
        self.label_box = self._load_label_box()   # where F7 last found "Potential"; None after a manual F8 box
        self.profile = active_cube_profile()
        self.lines = []
        self._running = True
        self._selector = None
        self._requests = queue.Queue()
        self.box_visible = True           # Hide box: purely visual, reads still use self.region
        self.looping = False              # F9: cube -> read -> check, until the target shows up
        self.target = None                # parsed by parse_potential_target
        self.beep_enabled = True
        self.tiers = [None, None, None]   # per line, see read_potential_tiers
        self.rolls = 0
        self._mouse = MouseLock()
        self._loop_thread = None
        self._build(host)
        def register_hotkeys():           # off the UI thread - see OverlayApp.run for why
            for key, action in (("f7", self._auto_locate), ("f8", self._start_selection),
                                ("f9", self._toggle_loop), ("f10", self._toggle_beep)):
                keyboard.add_hotkey(key, self._requests.put, args=(action,), suppress=True, trigger_on_release=True)
        threading.Thread(target=register_hotkeys, daemon=True, name="hotkeys").start()
        self._tick()

    # ---- UI ------------------------------------------------------------
    def _build(self, host):
        outer = tk.Frame(host, bg=UI_BG, padx=24, pady=8)
        outer.pack(fill="both", expand=True)
        self._outer = outer
        card = OverlayApp._card(outer)
        card.pack(fill="x")
        tk.Label(card, text="POTENTIAL", fg=UI_MUTED, bg=UI_SURFACE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
        # Each line: a small square in the tier colour (as in the game), then the text.
        self._line_labels, self._line_dots = [], []
        for _ in range(3):
            row = tk.Frame(card, bg=UI_SURFACE)
            row.pack(fill="x", padx=14, pady=(0, 2))
            dot = tk.Canvas(row, width=12, height=12, bg=UI_SURFACE, highlightthickness=0)
            dot.pack(side="left", padx=(0, 10))
            dot_id = dot.create_rectangle(1, 1, 11, 11, fill=UI_SURFACE, outline=UI_BORDER)
            lbl = tk.Label(row, text="-", fg=UI_TEXT, bg=UI_SURFACE, font=("Segoe UI", 13, "bold"), anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            self._line_labels.append(lbl)
            self._line_dots.append((dot, dot_id))
        self._status = tk.Label(card, text="", fg=UI_MUTED, bg=UI_SURFACE, font=("Segoe UI", 9), anchor="w")
        self._status.pack(fill="x", padx=14, pady=(4, 10))

        total = OverlayApp._card(outer)
        total.pack(fill="x", pady=(12, 0))
        tk.Label(total, text="TOTAL", fg=UI_MUTED, bg=UI_SURFACE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
        self._total_label = tk.Label(total, text="-", fg=UI_TEXT, bg=UI_SURFACE,
                                     font=("Segoe UI", 12, "bold"), anchor="w", justify="left")
        self._total_label.pack(fill="x", padx=14, pady=(0, 10))

        cube = OverlayApp._card(outer)
        cube.pack(fill="x", pady=(12, 0))
        cube_row = tk.Frame(cube, bg=UI_SURFACE)
        cube_row.pack(fill="x", padx=14, pady=10)
        tk.Label(cube_row, text="CUBE TYPE", fg=UI_MUTED, bg=UI_SURFACE,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 14))
        self._cube_var = tk.StringVar(value=next((k for k, v in CUBE_PROFILES.items() if v is self.profile), CUBE_TYPE_DEFAULT))
        for key, prof in CUBE_PROFILES.items():
            tk.Radiobutton(cube_row, text=prof.name, value=key, variable=self._cube_var, bg=UI_SURFACE, fg=UI_TEXT,
                           selectcolor=UI_BG, activebackground=UI_SURFACE, activeforeground=UI_TEXT,
                           highlightthickness=0, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 12))
        self._cube_note = tk.Label(cube_row, text="", fg=UI_PRIMARY, bg=UI_SURFACE, font=("Segoe UI", 9))
        self._cube_note.pack(side="left")
        self._cube_var.trace_add("write", lambda *_: self._set_cube_type())
        self._set_cube_type()

        goal = OverlayApp._card(outer)
        goal.pack(fill="x", pady=(12, 0))
        tk.Label(goal, text="LOOKING FOR", fg=UI_MUTED, bg=UI_SURFACE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
        saved = self._load_target()
        stat_names = [name for name, _, _ in POTENTIAL_STATS]
        # Mode: 'total' (one stat, reach a number) or 'combo' (three lines from a set).
        saved_modes = saved.get("modes") if isinstance(saved.get("modes"), list) else [saved.get("mode", "total")]
        self._mode_vars = {k: tk.BooleanVar(value=k in saved_modes) for k in ("total", "combo", "perstat")}
        if not any(v.get() for v in self._mode_vars.values()):
            self._mode_vars["total"].set(True)
        mode_row = tk.Frame(goal, bg=UI_SURFACE)
        mode_row.pack(fill="x", padx=14, pady=(0, 6))
        for value, text in (("total", "Reach a total"), ("combo", "Combination of stats"), ("perstat", "Lines per stat")):
            tk.Checkbutton(mode_row, text=text, variable=self._mode_vars[value], bg=UI_SURFACE, fg=UI_TEXT,
                           selectcolor=UI_BG, activebackground=UI_SURFACE, activeforeground=UI_TEXT,
                           highlightthickness=0, font=("Segoe UI", 10)).pack(side="left", padx=(0, 14))
        self._all_stats_var = tk.BooleanVar(value=bool(saved.get("all_stats", True)))
        tk.Checkbutton(mode_row, text="All Stats counts as STR / DEX / INT / LUK", variable=self._all_stats_var,
                       bg=UI_SURFACE, fg=UI_MUTED, selectcolor=UI_BG, activebackground=UI_SURFACE,
                       activeforeground=UI_TEXT, highlightthickness=0, font=("Segoe UI", 9)).pack(side="right")
        self._all_stats_var.trace_add("write", lambda *_: self._parse_target())
        goal_row = tk.Frame(goal, bg=UI_SURFACE)
        goal_row.pack(fill="x", padx=14, pady=(0, 10))
        self._goal_row = goal_row
        saved_leg = saved.get("legendary") if isinstance(saved.get("legendary"), dict) else {}
        self._leg_vars = {k: tk.BooleanVar(value=bool(saved_leg.get(k, False))) for k in ("total", "combo", "perstat")}

        def leg_box(parent, key, **pack):
            tk.Checkbutton(parent, text="all legendary", variable=self._leg_vars[key], bg=UI_SURFACE, fg=UI_MUTED,
                           selectcolor=UI_BG, activebackground=UI_SURFACE, activeforeground=UI_TEXT,
                           highlightthickness=0, font=("Segoe UI", 9)).pack(**pack)
            self._leg_vars[key].trace_add("write", lambda *_: self._parse_target())
        self._leg_box = leg_box
        self._stat_var = tk.StringVar(value=saved.get("stat") if saved.get("stat") in stat_names else stat_names[0])
        self._min_var = tk.StringVar(value=str(saved.get("min", "")))
        style = ttk.Style(self.root)
        style.configure("Cubes.TMenubutton", background=UI_BG, foreground=UI_TEXT, arrowcolor=UI_ACCENT,
                        font=("Segoe UI", 11), padding=(10, 6), borderwidth=0)
        style.map("Cubes.TMenubutton", background=[("active", UI_BUTTON)])
        stat_menu = ttk.OptionMenu(goal_row, self._stat_var, self._stat_var.get(), *stat_names, style="Cubes.TMenubutton")
        stat_menu.pack(side="left", fill="x", expand=True)
        stat_menu["menu"].config(bg=UI_SURFACE, fg=UI_TEXT, activebackground=UI_BUTTON, activeforeground=UI_TEXT,
                                 font=("Segoe UI", 10), bd=0)
        tk.Label(goal_row, text=">=", fg=UI_MUTED, bg=UI_SURFACE, font=("Segoe UI", 11)).pack(side="left", padx=8)
        min_entry = tk.Entry(goal_row, textvariable=self._min_var, width=5, bg=UI_BG, fg=UI_TEXT,
                             insertbackground=UI_TEXT, relief="flat", font=("Segoe UI", 12), justify="center",
                             highlightbackground=UI_BORDER, highlightcolor=UI_ACCENT, highlightthickness=1)
        min_entry.pack(side="left", ipady=5)
        self._unit_label = tk.Label(goal_row, text="%", fg=UI_MUTED, bg=UI_SURFACE, font=("Segoe UI", 11))
        self._unit_label.pack(side="left", padx=(4, 10))
        self._leg_box(goal_row, "total", side="left", padx=(6, 0))
        # Combination: a checkbox per stat, in a grid under the mode switch.
        combo = tk.Frame(goal, bg=UI_SURFACE)
        self._combo_frame = combo
        saved_combo = set(saved.get("combo", []))
        count_row = tk.Frame(combo, bg=UI_SURFACE)
        count_row.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        tk.Label(count_row, text="At least", fg=UI_MUTED, bg=UI_SURFACE, font=("Segoe UI", 10)).pack(side="left")
        self._count_var = tk.StringVar(value=str(saved.get("count", 3)) if str(saved.get("count", 3)) in ("1", "2", "3") else "3")
        for n in ("1", "2", "3"):
            tk.Radiobutton(count_row, text=n, value=n, variable=self._count_var, bg=UI_SURFACE, fg=UI_TEXT,
                           selectcolor=UI_BG, activebackground=UI_SURFACE, activeforeground=UI_TEXT,
                           highlightthickness=0, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(6, 0))
        tk.Label(count_row, text="of the 3 lines are one of:", fg=UI_MUTED, bg=UI_SURFACE,
                 font=("Segoe UI", 10)).pack(side="left", padx=(8, 0))
        self._leg_box(count_row, "combo", side="left", padx=(16, 0))
        self._count_var.trace_add("write", lambda *_: self._parse_target())
        self._combo_vars = {}
        for i, name in enumerate(stat_names):
            var = tk.BooleanVar(value=name in saved_combo)
            self._combo_vars[name] = var
            tk.Checkbutton(combo, text=name, variable=var, bg=UI_SURFACE, fg=UI_TEXT, selectcolor=UI_BG,
                           activebackground=UI_SURFACE, activeforeground=UI_TEXT, highlightthickness=0,
                           font=("Segoe UI", 10), anchor="w").grid(row=1 + i // 3, column=i % 3, sticky="w", padx=(0, 12), pady=1)
            var.trace_add("write", lambda *_: self._parse_target())
        # Lines per stat: a 0/1/2/3 picker beside every stat. 0 = not required.
        perstat = tk.Frame(goal, bg=UI_SURFACE)
        self._perstat_frame = perstat
        saved_needs = saved.get("needs", {}) if isinstance(saved.get("needs"), dict) else {}
        head = tk.Frame(perstat, bg=UI_SURFACE)
        head.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        tk.Label(head, text="Lines required, per stat (0 = not needed):", fg=UI_MUTED, bg=UI_SURFACE,
                 font=("Segoe UI", 10)).pack(side="left")
        self._leg_box(head, "perstat", side="left", padx=(16, 0))
        self._perstat_vars = {}
        for i, name in enumerate(stat_names):
            row, col = 1 + i // 2, (i % 2) * 2
            var = tk.StringVar(value=str(saved_needs.get(name, 0)) if str(saved_needs.get(name, 0)) in ("0", "1", "2", "3") else "0")
            self._perstat_vars[name] = var
            tk.Label(perstat, text=name, fg=UI_TEXT, bg=UI_SURFACE, font=("Segoe UI", 10), anchor="w",
                     width=15).grid(row=row, column=col, sticky="w", pady=1)
            picks = tk.Frame(perstat, bg=UI_SURFACE)
            picks.grid(row=row, column=col + 1, sticky="w", padx=(0, 18))
            for n in ("0", "1", "2", "3"):
                tk.Radiobutton(picks, text=n, value=n, variable=var, bg=UI_SURFACE, fg=UI_TEXT, selectcolor=UI_BG,
                               activebackground=UI_SURFACE, activeforeground=UI_TEXT, highlightthickness=0,
                               font=("Segoe UI", 9, "bold"), padx=2).pack(side="left")
            var.trace_add("write", lambda *_: self._parse_target())
        self._target_label = tk.Label(goal, text="", fg=UI_MUTED, bg=UI_SURFACE, font=("Segoe UI", 9), anchor="w")
        self._target_label.pack(fill="x", padx=14, pady=(0, 10))
        for var in (self._stat_var, self._min_var, *self._mode_vars.values()):
            var.trace_add("write", lambda *_: self._parse_target())

        row = tk.Frame(outer, bg=UI_BG)
        row.pack(fill="x", pady=(12, 0))
        OverlayApp._button(row, "Auto-locate (F7)", self._auto_locate, UI_BUTTON, UI_TEXT).pack(
            side="left", expand=True, fill="x", padx=(0, 6))
        OverlayApp._button(row, "Choose region (F8)", self._start_selection, UI_BUTTON, UI_TEXT).pack(
            side="left", expand=True, fill="x", padx=(0, 6))
        OverlayApp._button(row, "Read", self._read, UI_BUTTON, UI_TEXT).pack(
            side="left", expand=True, fill="x", padx=(0, 6))
        self._loop_button = OverlayApp._button(row, "Start (F9)", self._toggle_loop, UI_PRIMARY, UI_BG)
        self._loop_button.pack(side="left", expand=True, fill="x")
        row2 = tk.Frame(outer, bg=UI_BG)
        row2.pack(fill="x", pady=(6, 0))
        self._box_button = OverlayApp._button(row2, "Hide box", self._toggle_box, UI_BUTTON, UI_TEXT)
        self._box_button.pack(side="left")
        tk.Label(outer, text="F7 finds the Potential panel and boxes its three lines (F8 draws the box by hand). "
                             "F9 starts cubing: click, Enter, Enter, wait for the new lines, read them, stop with a "
                             "beep once your goal is met. F9 again stops. F10 mutes the beep.",
                 fg=UI_MUTED, bg=UI_BG, font=("Segoe UI", 9), wraplength=460,
                 justify="left").pack(anchor="w", pady=(10, 0))
        self._parse_target()
        # Same overlay as Flames: a click-through-transparent window with a
        # coloured frame you drag to move and corner handles you drag to
        # resize. The frame is drawn outside the capture region so it is never
        # in the crop the OCR reads.
        self.overlay = tk.Toplevel(self.root)
        self.overlay.overrideredirect(True)
        self.overlay.attributes("-topmost", True)
        self.overlay.attributes("-transparentcolor", "black")
        self.overlay.configure(bg="black")
        self._canvas = tk.Canvas(self.overlay, bg="black", highlightthickness=0, cursor="fleur")
        self._canvas.pack()
        self._rect = self._canvas.create_rectangle(0, 0, 0, 0, outline=self.COLOR,
                                                   width=self.BORDER * 2, fill="black", tags="border")
        self._handles = {c: self._canvas.create_rectangle(0, 0, 0, 0, fill=self.COLOR, outline="",
                                                          tags=("handle", f"handle_{c}"))
                         for c in ("nw", "ne", "sw", "se")}
        self._drag = {}
        self._canvas.tag_bind("border", "<ButtonPress-1>", self._on_move_press)
        self._canvas.tag_bind("border", "<B1-Motion>", self._on_move_drag)
        self._canvas.tag_bind("border", "<ButtonRelease-1>", self._on_release)
        for c in self._handles:
            self._canvas.tag_bind(f"handle_{c}", "<ButtonPress-1>", lambda e, c=c: self._on_resize_press(e, c))
            self._canvas.tag_bind(f"handle_{c}", "<B1-Motion>", self._on_resize_drag)
            self._canvas.tag_bind(f"handle_{c}", "<ButtonRelease-1>", self._on_release)
        self._sync_overlay()
        self._set_status("Ready." if self.region else
                         "No box yet - press F7 with the Potential window open, or F8 to draw one.")

    def _sync_overlay(self):
        if not self.region:
            self.overlay.withdraw()
            return
        left, top, w, h = self.region
        b, hs = self.BORDER, self.HANDLE
        W, H = w + 2 * b, h + 2 * b
        self.overlay.geometry(f"{W}x{H}+{left - b}+{top - b}")
        self._canvas.config(width=W, height=H)
        self._canvas.coords(self._rect, b, b, w + b, h + b)
        for c, item in self._handles.items():
            x = 0 if "w" in c else W - hs
            y = 0 if "n" in c else H - hs
            self._canvas.coords(item, x, y, x + hs, y + hs)
        if self.box_visible:
            self.overlay.deiconify()
        else:
            self.overlay.withdraw()

    # ---- drag to move, corners to resize (same scheme as OverlayApp) ----
    def _on_move_press(self, event):
        self._drag = {"mode": "move", "sx": event.x_root, "sy": event.y_root, "orig": tuple(self.region)}

    def _on_move_drag(self, event):
        d = self._drag
        if d.get("mode") != "move":
            return
        ox, oy, w, h = d["orig"]
        self.region = (ox + event.x_root - d["sx"], oy + event.y_root - d["sy"], w, h)
        self.overlay.geometry(f"+{self.region[0] - self.BORDER}+{self.region[1] - self.BORDER}")

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
        c = d["corner"]
        if "n" in c:
            y1 = min(y1 + dy, y2 - self.MIN_SIZE)
        if "s" in c:
            y2 = max(y2 + dy, y1 + self.MIN_SIZE)
        if "w" in c:
            x1 = min(x1 + dx, x2 - self.MIN_SIZE)
        if "e" in c:
            x2 = max(x2 + dx, x1 + self.MIN_SIZE)
        self.region = (x1, y1, x2 - x1, y2 - y1)
        self._sync_overlay()

    def _on_release(self, _event):
        if self._drag:
            self._drag = {}
            self._save_region()
            self._set_status(f"Box: {self.region}")

    def _set_status(self, text):
        self._status.config(text=text)

    def _set_cube_type(self):
        """Switch the active profile: which label F7 anchors on and where the
        lines/icons/cube row sit. Persisted so the readers pick it up too."""
        key = self._cube_var.get()
        self.profile = CUBE_PROFILES.get(key, CUBE_PROFILES[CUBE_TYPE_DEFAULT])
        self._cube_note.config(text="")
        try:
            data = json.loads(MODE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data["cube_type"] = key
        try:
            MODE_FILE.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def _toggle_box(self):
        """Show/hide the on-screen box - visual only, like Flames' Hide box:
        reads screenshot self.region directly and never need the overlay."""
        self.box_visible = not self.box_visible
        self._box_button.config(text="Hide box" if self.box_visible else "Show box")
        self._sync_overlay()

    def _tick(self):
        if not self._running:
            return
        while not self._requests.empty():
            self._requests.get_nowait()()
        self.root.after(int(RENDER_INTERVAL * 1000), self._tick)

    # ---- region ----------------------------------------------------------
    @staticmethod
    def _load_region():
        try:
            r = json.loads(CUBES_REGION_FILE.read_text(encoding="utf-8")).get("region")
            return tuple(int(v) for v in r) if r and len(r) == 4 else None
        except Exception:
            return None

    @staticmethod
    def _load_label_box():
        try:
            r = json.loads(CUBES_REGION_FILE.read_text(encoding="utf-8")).get("label")
            return tuple(int(v) for v in r) if r and len(r) == 4 else None
        except Exception:
            return None

    def _save_region(self):
        try:
            CUBES_REGION_FILE.write_text(json.dumps({"region": list(self.region) if self.region else None,
                                                     "label": list(self.label_box) if self.label_box else None}),
                                         encoding="utf-8")
        except Exception:
            pass

    def _cubes_left(self):
        """False when the material row shows no cyan-highlighted slot - the
        selected cube type ran out. True if a highlight is there, and also
        True when we can't check (box placed by hand with F8, so no label
        position is known): never stop a run on a guess."""
        if not self.label_box:
            return True
        if self.profile.cubes_left == "count":
            lx, ly, sw, sh = self.label_box
            dx, dy, w, h = self.profile.count_box
            box = (round(lx + dx * sw), round(ly + dy * sh), round(w * sw), round(h * sh))
            try:
                img = _grab(box)
                big = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)
                txt = pytesseract.image_to_string(big, config="--psm 7 -c tessedit_char_whitelist=0123456789").strip()
            except Exception:
                return True
            # Unreadable = don't stop on a guess; a clean "0" = out of cubes.
            return not (txt.isdigit() and int(txt) == 0)
        strip = self.profile.row_for(self.label_box)
        try:
            hsv = np.array(_grab(strip).convert("HSV")).astype(int)
        except Exception:
            return True
        cyan = (hsv[..., 0] > 120) & (hsv[..., 0] < 140) & (hsv[..., 1] > 120) & (hsv[..., 2] > 170)
        return int(cyan.sum()) >= CUBES_HIGHLIGHT_MIN_PX

    @staticmethod
    def _load_target():
        try:
            data = json.loads(CUBES_TARGET_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _describe(target):
        return target.describe()

    def _wrap(self, goal, key):
        return LegendaryOnly(goal) if self._leg_vars[key].get() else goal

    def _parse_target(self):
        """Compose the (words, minimum, wants_percent, tier) target from the
        pickers - the same tuple parse_potential_target produced from typed
        text, so the loop and matcher are unchanged. A blank minimum with a
        is no target at all. Tier is always None: the tier squares are
        informational, the goal is the numbered total."""
        global ALL_STATS_COUNTS
        ALL_STATS_COUNTS = bool(self._all_stats_var.get())
        name = self._stat_var.get()
        unit = next((u for n, _w, u in POTENTIAL_STATS if n == name), "%")
        self._unit_label.config(text=unit)
        raw = self._min_var.get().strip().rstrip("%")
        modes = [k for k, v in self._mode_vars.items() if v.get()]
        chosen = [n for n, v in self._combo_vars.items() if v.get()]
        needs = {n: int(v.get()) for n, v in self._perstat_vars.items() if v.get() != "0"}
        frames = {"total": self._goal_row, "combo": self._combo_frame, "perstat": self._perstat_frame}
        for key, frame in frames.items():
            frame.pack_forget()
        for key in ("total", "combo", "perstat"):
            if key in modes:
                frames[key].pack(fill="x", padx=14, pady=(0, 10 if key == "total" else 8), before=self._target_label)
        goals, problems = [], []
        if "total" in modes:
            if raw.isdigit() and int(raw) > 0:
                goals.append(self._wrap(TotalGoal(name, int(raw), unit), "total"))
            else:
                problems.append("enter a minimum value for the total")
        if "combo" in modes:
            if chosen:
                goals.append(self._wrap(ComboGoal(chosen, int(self._count_var.get())), "combo"))
            else:
                problems.append("tick the stats for the combination")
        if "perstat" in modes:
            total_needed = sum(needs.values())
            if not needs:
                problems.append("set how many lines of each stat you need")
            elif total_needed > 3:
                problems.append(f"lines per stat needs {total_needed} lines - an item only has 3")
            else:
                goals.append(self._wrap(PerStatGoal(needs), "perstat"))
        if not modes:
            self.target = None
            self._target_label.config(text="Tick at least one way of cubing.", fg=UI_MUTED)
        elif problems:
            self.target = None
            self._target_label.config(text="; ".join(problems).capitalize() + ".", fg=UI_PRIMARY)
        else:
            self.target = goals[0] if len(goals) == 1 else AnyOfGoal(goals)
            self._target_label.config(text="Stop when: " + self.target.describe(), fg=UI_ACCENT)
        try:
            CUBES_TARGET_FILE.write_text(json.dumps({"modes": modes, "stat": name, "min": raw, "combo": chosen,
                                                     "count": int(self._count_var.get()), "needs": needs,
                                                     "all_stats": ALL_STATS_COUNTS,
                                                     "legendary": {k: v.get() for k, v in self._leg_vars.items()}}),
                                         encoding="utf-8")
        except Exception:
            pass

    def _start_selection(self):
        if self._selector is not None:
            return
        self.overlay.withdraw()
        self._set_status("Drag a box over the three Potential lines.")
        self._selector = RegionSelector(parent=self.root, on_done=self._finish_selection)

    def _finish_selection(self, region):
        self._selector = None
        if region is not None:
            self.region = tuple(int(v) for v in region)
            self.label_box = None      # hand-placed: the cube-row check can't locate the row, so it stays off
            self._save_region()
            self._set_status(f"Box set: {self.region}")
        self._sync_overlay()

    def _auto_locate(self):
        self._set_status("Auto-locate: scanning the screen...")
        self.root.update_idletasks()
        self.overlay.withdraw()
        try:
            with mss.MSS() as sct:
                desktop = sct.monitors[0]
                shot = sct.grab(desktop)
            full_gray = np.array(Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").convert("L"))
            best, peaks, shape = locate_label(full_gray, self.profile.label_gray(),
                                              max_peaks=4 if self.profile.pick_label == "rightmost" else 1)
        except Exception as error:
            self._set_status(f"Auto-locate failed: {error}")
            self._sync_overlay()
            return
        if not peaks:
            self._set_status(f"Auto-locate: Potential panel not found (best match {best:.2f}).")
            self._sync_overlay()
            return
        sh, sw = shape
        x, y = max(peaks) if self.profile.pick_label == "rightmost" else peaks[0]
        self.label_box = (desktop["left"] + x, desktop["top"] + y, sw, sh)
        self._save_region()
        self.region = self.profile.block_for((desktop["left"] + x, desktop["top"] + y), (sh, sw))
        self._save_region()
        self._sync_overlay()
        self._set_status(f"Found the Potential panel (match {best:.2f}). Press F9 to read.")
        self._read()

    # ---- read --------------------------------------------------------------
    def _read(self):
        """One read from the UI thread (F7 / the button)."""
        if not self.region:
            self._set_status("No box yet - press F7 or F8 first.")
            return
        # Hide the box while grabbing so its own border is not in the crop (the
        # Flames smudge lesson), then bring it back.
        self.overlay.withdraw()
        self.root.update_idletasks()
        try:
            img = _grab(self.region)
            self.tiers, self.lines = read_potential_tiers(img, self.profile), read_potential_lines(img, self.profile)
        except Exception as error:
            self._set_status(f"Read failed: {error}")
            self.tiers, self.lines = [None] * 3, []
        finally:
            self._sync_overlay()
        self._show_lines()

    def _show_lines(self):
        for lbl, (dot, dot_id), entry, tier in zip(self._line_labels, self._line_dots,
                                                    self.lines + [None] * 3, self.tiers + [None] * 3):
            colour = POTENTIAL_TIER_COLOURS.get(tier)
            dot.itemconfig(dot_id, fill=colour or UI_SURFACE, outline=colour or UI_BORDER)
            if entry is None:
                lbl.config(text="-", fg=UI_MUTED)
            elif entry[1] is None:
                lbl.config(text=f"? {entry[0]}", fg=UI_MUTED)
            else:
                lbl.config(text=f"{entry[0]}  {entry[1]}", fg=UI_TEXT)
        good = [e for e in self.lines if e[1] is not None]
        totals = total_potential_lines(good)
        text = "\n".join(f"{stat}  {value}" for stat, value in totals) if totals else "-"
        if self.target is not None and good:
            text += "\n\u2192 " + self.target.progress(self.lines, self.tiers)
        self._total_label.config(text=text)
        self._set_status(f"Read {len(good)} line(s)." if good else "Nothing readable in the box.")

    # ---- cube loop -------------------------------------------------------------
    def _toggle_beep(self):
        self.beep_enabled = not self.beep_enabled
        self._set_status("Beep muted." if not self.beep_enabled else "Beep on.")

    def _toggle_loop(self):
        if self.looping:
            self._stop_loop("Stopped.")
            return
        if not self.region:
            self._set_status("No box yet - press F7 or F8 first.")
            return
        if self.target is None:
            self._set_status("Set what you are looking for first.")
            return
        if self.root.focus_displayof() is not None:
            self._set_status("Focus the game, then press F9.")
            return
        self.looping = True
        self.rolls = 0
        self._loop_button.config(text="Stop (F9)")
        if self.discord is not None and self.discord.enabled and self._discord_modes()["cubes"]:
            own = []
            for w in (self.root, self.overlay):
                try:
                    own.append(w.winfo_id())
                except (AttributeError, tk.TclError):
                    pass
            self._activation_target = bind_foreground_target(self.region, own, report=self._set_status)
        self.overlay.withdraw()          # the box must not be in the crops the loop takes
        self.root.update_idletasks()     # ...so make sure it is actually gone before the first grab
        self._mouse.lock()
        self._loop_thread = threading.Thread(target=self._cube_loop, daemon=True, name="cube-loop")
        self._loop_thread.start()
        self._set_status(f"Cubing until {self._describe(self.target)} ...")

    def _stop_loop(self, message):
        self.looping = False
        self._mouse.unlock()
        self._loop_button.config(text="Start (F9)")
        self._sync_overlay()
        self._set_status(message)

    def _press_sequence(self):
        """One cube: left click, Enter, Enter, with a small jittered gap."""
        pydirectinput.click()
        self._mouse.reassert()
        for _ in range(2):
            time.sleep(OverlayApp._jittered(CUBES_PRESS_GAP))
            if not self.looping:
                return
            pydirectinput.press("enter")

    def _cube_loop(self):
        """Watch thread. Read the current lines first (an item that already
        matches is never rolled away). Then: spam on, poll the box until its
        pixels change (the panel redrew with a new roll), spam OFF, OCR the
        new lines, check the total; on a miss, spam on again. Everything
        UI-facing goes back to the Tk thread through the request queue."""
        target = self.target
        time.sleep(0.1)                  # let the overlay's hide land on screen before the first grab
        img = _grab(self.region)
        first = True
        while self.looping and self._running:
            if not first:
                if not self._cubes_left():
                    self._requests.put(lambda: self._stop_loop(
                        f"Stopped after {self.rolls} roll(s): no cubes left (no cube selected in the material row)."))
                    return
                before = np.asarray(img.convert("L"), dtype=np.int16)
                self._press_sequence()
                deadline = time.perf_counter() + CUBES_CHANGE_TIMEOUT
                img = None
                while self.looping and self._running and time.perf_counter() < deadline:
                    time.sleep(CUBES_CHANGE_POLL)
                    cur_img = _grab(self.region)
                    cur = np.asarray(cur_img.convert("L"), dtype=np.int16)
                    if cur.shape == before.shape and (np.abs(cur - before) > 40).mean() >= CUBES_CHANGE_FRAC:
                        img = cur_img
                        break
                if not self.looping:
                    break
                if img is None:
                    self._requests.put(lambda: self._stop_loop(
                        f"Stopped after {self.rolls} roll(s): the panel did not change "
                        f"(out of cubes, or the dialog closed?)."))
                    return
                # The redraw may still be animating on the first differing frame -
                # wait for the pixels to hold still before trusting the OCR.
                img = self._settle(img)
                self.rolls += 1
            first = False
            try:
                tiers, lines = read_potential_tiers(img, self.profile), read_potential_lines(img, self.profile)
            except Exception:
                tiers, lines = [None] * 3, []
            found = target.check(lines, tiers)
            hit = (found, "") if found else None
            self._requests.put(lambda tiers=tiers, lines=lines: (setattr(self, "tiers", tiers),
                                                                 setattr(self, "lines", lines), self._show_lines()))
            if hit is not None:
                self._requests.put(lambda hit=hit: self._on_hit(hit))
                return
            self._requests.put(lambda: self._set_status(
                f"Roll {self.rolls}: no match yet." if self.rolls else "Current item doesn't match - cubing..."))

    def _settle(self, img):
        """Return a grab taken once two consecutive polls agree (the panel has
        finished redrawing), or the latest grab after CUBES_SETTLE_MAX."""
        prev = np.asarray(img.convert("L"), dtype=np.int16)
        deadline = time.perf_counter() + CUBES_SETTLE_MAX
        while self.looping and self._running and time.perf_counter() < deadline:
            time.sleep(CUBES_CHANGE_POLL)
            img = _grab(self.region)
            cur = np.asarray(img.convert("L"), dtype=np.int16)
            if cur.shape == prev.shape and (np.abs(cur - prev) > 40).mean() < CUBES_CHANGE_FRAC:
                return img
            prev = cur
        return img

    def _on_hit(self, hit):
        rank = (self.tiers[0] or "").capitalize()
        summary = f"{rank} {hit[0]} {hit[1]}".replace("  ", " ").strip()
        tail = "" if self.profile.commit_on_match else " - it's showing in AFTER; close the dialog to keep it (Reset would roll it away)."
        self._stop_loop(f"Got it after {self.rolls} roll(s): {summary}{tail}")
        if self.beep_enabled:
            threading.Thread(target=beep, daemon=True).start()
        if self.discord is not None and self.discord.enabled and self._discord_modes()["cubes"]:
            target = self._activation_target
            if target is not None and not target_is_current(target):
                target = None
            lines = "; ".join(f"{s} {v}" for s, v in self.lines if v) or "no lines read"
            self.discord.schedule(f"{summary} after {self.rolls} roll(s) [{lines}]", target)
        self._activation_target = None

    # ---- lifecycle -----------------------------------------------------------
    def stop(self):
        if not self._running:
            return
        self._running = False
        self.looping = False
        self._mouse.unlock()
        for key in ("f7", "f8", "f9", "f10"):
            try:
                keyboard.remove_hotkey(key)
            except (KeyError, ValueError):
                pass
        if self._selector is not None:
            try:
                self._selector.root.destroy()
            except Exception:
                pass
            self._selector = None
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
        if self._outer.winfo_exists():
            self._outer.destroy()


def build_cubes(host, discord=None, discord_modes=None):
    return CubesApp(host, discord=discord, discord_modes=discord_modes)


def run_app(args):
    """One window, two tabs - Flames (the reset-dialog watcher) and Cubes.
    Only one mode runs at a time: switching tabs stops the current mode
    (hotkeys, threads, overlay - see OverlayApp.stop) and starts the other
    inside its tab. The last tab is remembered and reopened next launch."""
    root = tk.Tk()
    root.title(f"Maple Tapper Looker {APP_VERSION}")
    root.configure(bg=UI_BG)
    root.resizable(False, False)
    root.option_add("*Font", ("Segoe UI", 10))
    icon, logo = logo_photo(256, root), logo_photo(144, root)
    root.iconphoto(True, icon)
    root._header_images = (icon, logo)          # keep references alive for the window's lifetime

    top = tk.Frame(root, bg=UI_BG, padx=24)
    top.pack(fill="x", pady=(16, 0))
    build_header(top, logo)

    # Discord: one notifier for the whole app, with a switch per tab. The
    # notifier's own enabled flag follows "any tab on"; each tab checks its
    # own switch before scheduling an alert.
    shell_log = {"fn": print}
    discord = DiscordNotifier(log=lambda msg: shell_log["fn"](msg))
    discord_flags = load_discord_modes()
    strip = tk.Frame(top, bg=UI_SURFACE, highlightbackground=UI_BORDER, highlightthickness=1)
    strip.pack(fill="x", pady=(0, 6))
    tk.Label(strip, text="DISCORD ALERTS", fg=UI_MUTED, bg=UI_SURFACE,
             font=("Segoe UI", 9, "bold")).pack(side="left", padx=(14, 16), pady=8)
    mode_vars = {}

    def apply_modes(*_):
        for key, var in mode_vars.items():
            discord_flags[key] = bool(var.get())
        save_discord_modes(discord_flags)
        want = any(discord_flags.values())
        if want != discord.enabled:
            try:
                if not discord.set_enabled(want) and want:
                    shell_log["fn"]("Set Discord webhook and user ID before enabling alerts.")
                    for var in mode_vars.values():
                        var.set(False)
                    for key in discord_flags:
                        discord_flags[key] = False
                    save_discord_modes(discord_flags)
            except OSError:
                shell_log["fn"]("Could not save Discord settings.")

    for key, text in (("flames", "Flames"), ("cubes", "Cubes")):
        var = tk.BooleanVar(root, value=discord_flags[key] and discord.enabled)
        mode_vars[key] = var
        tk.Checkbutton(strip, text=text, variable=var, command=apply_modes, bg=UI_SURFACE, fg=UI_TEXT,
                       selectcolor=UI_BG, activebackground=UI_SURFACE, activeforeground=UI_TEXT,
                       highlightthickness=0, font=("Segoe UI", 10, "bold"), cursor="hand2",
                       state="normal" if discord.supported else "disabled").pack(side="left", padx=(0, 10))
    OverlayApp._button(strip, "Discord settings",
                       lambda: open_discord_settings(root, discord, shell_log["fn"],
                                                     on_change=lambda: [v.set(False) for v in mode_vars.values()]),
                       UI_BUTTON, UI_TEXT).pack(side="right", padx=10, pady=4)
    discord_modes = lambda: discord_flags

    style = ttk.Style(root)
    style.theme_use("clam")
    # Conventional tab strip: a baseline the pages sit on, the active tab lifted
    # and merged into the page (same colour, no bottom edge), inactive tabs
    # recessed and dimmer, hover feedback, no focus ring. clam's default draws
    # each tab as a detached chip with a dotted focus rectangle on click.
    style.configure("Mode.TNotebook", background=UI_BG, borderwidth=0, tabmargins=(24, 10, 0, 0),
                    lightcolor=UI_BG, darkcolor=UI_BG, bordercolor=UI_BORDER)
    style.configure("Mode.TNotebook.Tab", background=UI_SURFACE, foreground=UI_MUTED,
                    padding=(26, 9), font=("Segoe UI", 11, "bold"), borderwidth=1,
                    lightcolor=UI_SURFACE, darkcolor=UI_SURFACE, bordercolor=UI_BORDER, focuscolor=UI_BG)
    style.map("Mode.TNotebook.Tab",
              background=[("selected", UI_BG), ("active", UI_BUTTON)],
              foreground=[("selected", UI_TEXT), ("active", UI_TEXT)],
              lightcolor=[("selected", UI_BG)], darkcolor=[("selected", UI_BG)],
              padding=[("selected", (26, 11))],          # lifted: a little taller than its neighbours
              expand=[("selected", (0, 0, 0, 2))])       # ...and overlapping the baseline so it merges
    style.layout("Mode.TNotebook.Tab", [("Notebook.tab", {"sticky": "nswe", "children": [
        ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
            ("Notebook.label", {"side": "top", "sticky": ""})]})]})])   # drop Notebook.focus -> no dotted ring
    notebook = ttk.Notebook(root, style="Mode.TNotebook")
    notebook.pack(fill="both", expand=True)
    tabs = {}
    for mode, title in (("flames", "Flames"), ("cubes", "Cubes")):
        tabs[mode] = tk.Frame(notebook, bg=UI_BG)
        notebook.add(tabs[mode], text=f"  {title}  ")
    modes = list(tabs)

    state = {"mode": None, "app": None}

    def start(mode):
        if state["app"] is not None:
            state["app"].stop()
        state["mode"], state["app"] = mode, None
        state["app"] = (run_flames(args, tabs["flames"], discord, discord_modes) if mode == "flames"
                        else build_cubes(tabs["cubes"], discord, discord_modes))
        # route the notifier's log lines into the active tab's own log
        shell_log["fn"] = getattr(state["app"], "_log", None) or (lambda m: state["app"]._set_status(m))
        save_mode(mode)
        if state.get("size") is None:
            # Size the window ONCE, for the taller tab, so switching never resizes
            # it and it doesn't open small on Cubes then jump when Flames starts.
            # Flames is the taller one; measure its content off-screen if we
            # didn't open on it.
            root.update_idletasks()
            if mode != "flames":
                # Build Flames' widgets only (no threads, no hotkeys, no OCR) into
                # its hidden tab, measure, tear them down again.
                probe = OverlayApp(load_region() or default_region(), enter_spam=False)
                probe._build_window(tabs["flames"])
                root.update_idletasks()
                h = root.winfo_reqheight()
                probe.overlay.destroy()
                probe._outer.destroy()
                root.update_idletasks()
            else:
                h = root.winfo_reqheight()
            state["size"] = (max(540, root.winfo_reqwidth()), h)
            root.geometry("%dx%d" % state["size"])
            root.minsize(*state["size"])

    def on_tab_changed(_event):
        mode = modes[notebook.index("current")]
        if mode != state["mode"]:
            start(mode)

    def on_close():
        if state["app"] is not None:
            state["app"].stop()
        discord.close()
        root.destroy()

    notebook.bind("<<NotebookTabChanged>>", on_tab_changed)
    root.protocol("WM_DELETE_WINDOW", on_close)
    notebook.select(tabs[load_mode() or "flames"])
    if state["app"] is None:            # selecting the already-current tab fires no event
        start(modes[notebook.index("current")])
    try:
        root.mainloop()
    except KeyboardInterrupt:
        on_close()

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
    parser.add_argument("--mode", choices=("flames", "cubes"),
                         help="open on this tab instead of the last one used")
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

    if args.mode:
        save_mode(args.mode)
    run_app(args)
