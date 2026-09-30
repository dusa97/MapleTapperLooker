"""Rebuild main.py's POTENTIAL_LINE_VALUES from tools/potential_lines.txt.

Run it after editing the table:

    python tools/build_line_values.py

The table says what each potential line can roll at unique and legendary. The
app uses it to refuse a read the game could not have produced - a unique INT
line is 9% or 10%, so a read of 70% is a misread and the panel is read again.

Rows look like:   Boss Damage | % | unique | 30, 35   (notes in brackets ignored)
Only unique and legendary rows are used; rare and epic are not worth checking.
"""
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "tools" / "potential_lines.txt"
MAIN = ROOT / "main.py"
START = "POTENTIAL_LINE_VALUES = {"


def read_table():
    """The table as {(name, tier, shape): (values...)}."""
    rows = {}
    for raw in io.open(TABLE, encoding="utf-8"):
        raw = raw.split("#")[0].strip()
        if not raw or "|" not in raw:
            continue
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 4:
            continue
        name, shape, tier, values = parts[0], parts[1], parts[2], parts[3]
        if tier not in ("unique", "legendary"):
            continue
        numbers = re.findall(r"-?\d+", re.sub(r"\(.*?\)", "", values))   # drop "(guess)" notes
        if numbers:
            rows[(name, tier, shape)] = tuple(sorted({int(n) for n in numbers}))
    return rows


def main():
    rows = read_table()
    if not rows:
        raise SystemExit(f"no usable rows in {TABLE}")
    block = [START]
    for key in sorted(rows):
        name, tier, shape = key
        block.append(f'    ("{name}", "{tier}", "{shape}"): {rows[key]},')
    block.append("}")
    new = "\n".join(block)

    text = io.open(MAIN, encoding="utf-8").read()
    start = text.index(START)
    end = text.index("\n}", start) + len("\n}")
    io.open(MAIN, "w", encoding="utf-8", newline="\n").write(text[:start] + new + text[end:])
    print(f"wrote {len(rows)} rows into main.py")
    for key in sorted(rows):
        print(f"  {key[0]:16s} {key[1]:10s} {key[2]:4s} {rows[key]}")


if __name__ == "__main__":
    main()
