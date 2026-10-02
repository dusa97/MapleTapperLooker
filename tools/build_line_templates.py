"""Build the stat-line template table from saved panel captures.

    python tools/build_line_templates.py

Why: the game draws its stat lines in a fixed bitmap font, so the same line
always makes the same picture once the background is masked away. Measured on
133 saved panels, 360 of 399 line images were an exact repeat of another. That
turns reading them into a lookup rather than an OCR problem - a thousandth of a
millisecond instead of 130 ms, and a match cannot be a misread.

How a line gets labelled: Tesseract reads it at four scales and has to agree
with itself at least twice, and the answer has to parse to a line the game can
really show - a known name, and a value the tier table allows. Anything failing
those tests is left out rather than guessed at, because a wrong template would
be believed forever.

The result goes to assets/line_templates.json and ships with the app. At
runtime the app adds to it as it meets new lines.
"""
import collections
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import main                                                    # noqa: E402

OUT = ROOT / "assets" / "line_templates.json"
SCALES = (3, 4, 5, 6)


band_key = main.line_band_key                  # one definition, shared with the app


def read_at(band, scale):
    big = band.resize((round(band.width * scale), round(band.height * scale)), Image.LANCZOS)
    hsv = np.array(big.convert("HSV"))
    ink = (hsv[..., 2].astype(int) > main.OCR_MASK_VALUE_MIN) & (hsv[..., 1].astype(int) < main.OCR_MASK_SATURATION_MAX)
    proc = Image.fromarray(np.where(ink, 0, 255).astype("uint8"), mode="L")
    return main.pytesseract.image_to_string(proc, config=main.POTENTIAL_OCR_CONFIG).strip().replace("\n", " ")


def parse(text):
    """The reading as (stat, value), or None when it is not a line the game can
    show. This is what keeps a bad template out of the table."""
    m = re.match(r"^([A-Za-z: ]+?)\s*:?\s*([+-]\s*\d+)\s*(%|sec)?$", text.strip())
    if m:
        stat = main._snap_stat_name(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", m.group(1)).strip(" :"))
        unit = m.group(3) or ""
        value = m.group(2).replace(" ", "") + (" sec" if unit == "sec" else unit)
        if stat in main.KNOWN_LINE_NAMES and not main._percent_is_impossible(value):
            return stat, value
        return None
    stat = main._snap_stat_name(text.strip())                   # a sentence line carries no number
    return (stat, None) if stat in main.KNOWN_LINE_NAMES else None


def build():
    captures = [f for f in sorted((ROOT / "debug_captures").glob("cubes_*.png"))
                if not f.name.endswith("_game.png") and "autolocate" not in f.name]
    if not captures:
        raise SystemExit("no captures in debug_captures/ to learn from")

    seen = collections.defaultdict(list)
    for f in captures:
        im = Image.open(f)
        if im.mode != "RGB":
            continue                                           # greyscale: the tier colours are gone
        im = im.convert("RGB")
        # Which cube type this panel came from: their boxes are different widths
        # (Bright ~202 px, Glowing ~250 px at this UI scale) and the band
        # geometry differs slightly, so the wrong guess would shift every band.
        name = "bright" if abs(im.width - 202) <= abs(im.width - 250) else "glowing"
        profile = main.CUBE_PROFILES[name]
        for line in range(3):
            key, band = band_key(im, profile, line)
            if key:
                seen[(f"{name}:{im.width}x{im.height}", key)].append(band)

    print(f"{len(captures)} captures -> {len(seen)} distinct line pictures")
    table, kept, skipped = {}, 0, collections.Counter()
    for (geometry, key), bands in sorted(seen.items()):
        votes = collections.Counter(read_at(bands[0], s) for s in SCALES)
        text, agree = votes.most_common(1)[0]
        if agree < 2:
            skipped["scales disagreed"] += 1
            continue
        parsed = parse(text)
        if not parsed:
            skipped[f"not a real line: {text[:22]}"] += 1
            continue
        table.setdefault(geometry, {})[key] = list(parsed)
        kept += 1

    # Merge, never overwrite: the app learns lines at runtime that no saved
    # picture covers, and a rebuild must not throw those away.
    OUT.parent.mkdir(exist_ok=True)
    merged = {}
    for source in (OUT, ROOT / "cubes_line_templates.json"):
        try:
            for geometry, entries in json.loads(source.read_text(encoding="utf-8"))["lines"].items():
                merged.setdefault(geometry, {}).update(entries)
        except Exception:
            pass
    for geometry, entries in table.items():
        merged.setdefault(geometry, {}).update(entries)
    table = merged
    OUT.write_text(json.dumps({"version": 1, "lines": table}, indent=1), encoding="utf-8")
    print(f"kept {kept} templates in {len(table)} geometries -> {OUT.relative_to(ROOT)}")
    for geometry, entries in table.items():
        print(f"  {geometry}: {len(entries)} lines")
    print("left out:")
    for why, n in skipped.most_common(10):
        print(f"  {n:3d}  {why}")


if __name__ == "__main__":
    build()
