"""Rebuild assets/digit_templates.npz from real captures in debug_captures/.

Run this after collecting more debug_captures data (see main.py's
DEBUG_CAPTURE_DIR) to improve/refresh the template-matching classifier used
by classify_number_via_templates(). This is deliberately a manual, offline
step rather than something the app does automatically — a bad or ambiguous
capture must never be able to silently degrade live detection on its own.

Usage:
    python tools/build_digit_templates.py

Only debug_captures/hit_*.png files are used: each one is a CONFIRMED
detection (passed the app's own two-independent-read confirmation before
ever being saved), so its filename-encoded label can be trusted as ground
truth. miss_*.png files are deliberately NOT used for training data - they
have no equivalent verification, and an earlier version of this script that
tried to source '-' sign templates from unverified negative captures ended
up corrupting classification of everything else (see main.py's
_is_plus_sign docstring for why the sign is checked as its own yes/no
question instead of as a template class).
"""
import sys
import glob
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import main as m
import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).parent.parent
HITS_DIR = REPO_ROOT / "debug_captures"
OUT_PATH = REPO_ROOT / "assets" / "digit_templates.npz"

# Hit captures whose logged value is known to NOT match what's actually in
# the image (from before read_delta was fixed to save the exact frame that
# produced a result, instead of a separate later screenshot that could show
# something different). Exclude these from training even if still present.
KNOWN_BAD = {
    "hit_20260914_231259_+633221.png",
    "hit_20260914_233154_+17971.png",
}

HIT_NAME_RE = re.compile(r"hit_\d+_\d+_([+-]?\d+)\.png")


def extract_labeled_glyphs(path: Path, label: str):
    """Segment *path*'s digits and pair each one with its expected
    character from *label*. Returns None if segmentation didn't produce
    exactly as many glyphs as the label has characters — such images are
    skipped rather than risk misaligned (wrong) training data."""
    img = Image.open(path).convert("RGB")
    mask = m.preprocess_for_ocr(img)
    boxes = m._segment_glyph_boxes(mask)
    glyphs = [b for b in boxes if not m._is_comma_box(b, mask.height)]
    chars = list(label)
    if len(glyphs) != len(chars):
        return None
    arr = np.array(mask)
    return [
        (ch, m._resize_for_template(arr[y0:y1, x0:x1]))
        for (x0, x1, y0, y1), ch in zip(glyphs, chars)
    ]


def main_build():
    hit_files = sorted(glob.glob(str(HITS_DIR / "hit_*.png")))
    templates_by_class = {}
    used, skipped = 0, 0
    for path_str in hit_files:
        path = Path(path_str)
        if path.name in KNOWN_BAD:
            continue
        match = HIT_NAME_RE.search(path.name)
        if not match:
            continue
        samples = extract_labeled_glyphs(path, match.group(1))
        if samples is None:
            skipped += 1
            continue
        used += 1
        for ch, glyph in samples:
            templates_by_class.setdefault(ch, []).append(glyph)

    print(f"Used {used} hit images ({skipped} skipped: segmentation didn't match label length).")
    print("Class counts:", {k: len(v) for k, v in templates_by_class.items()})

    if not templates_by_class:
        print("No usable training data found — leaving the existing template file untouched.")
        return

    save_dict = {}
    class_names = []
    for cls, samples in templates_by_class.items():
        key = {"+": "plus", "-": "minus"}.get(cls, cls)
        save_dict[key] = np.stack(samples).astype("uint8")
        class_names.append(cls)
    OUT_PATH.parent.mkdir(exist_ok=True)
    np.savez_compressed(OUT_PATH, class_names=np.array(class_names), **save_dict)
    print(f"Saved templates to {OUT_PATH}")


if __name__ == "__main__":
    main_build()
