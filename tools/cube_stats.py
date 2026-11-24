"""Cube statistics that remember where they got to.

log.txt only grows, and re-reading two million characters to recount the same
rolls gets slower every week. So this reads only the part it has not seen yet,
folds each finished session into tools/cube_stats.json, and reports from that
store. Run it as often as you like; the answer is the same either way.

    python tools/cube_stats.py                  ingest what is new, then report
    python tools/cube_stats.py --ingest         ingest only, no report
    python tools/cube_stats.py --report         report only, read nothing
    python tools/cube_stats.py --cube glowing   one cube type
    python tools/cube_stats.py --item hat       one item slot
    python tools/cube_stats.py --list           one line per stored session
    python tools/cube_stats.py --rebuild        forget the cursor, read it all again

Nothing is pooled across cube types, item slots or goals unless you ask for it
with --all: Glowing and Bright have their own line and tier odds, and every
item slot has its own potential pool, so one table over two of them describes
neither. The report says which group it picked and what else was in range.

Two things the log cannot tell us, which the store therefore records:

* The item slot. The app only began logging it on 2026-10-19. Earlier sessions
  carry none, and every one of those was a secondary - so that is what they are
  filed as, and the report says how many were assumed rather than read.
* The date. Added to the session header on the same day. Sessions older than
  that are given the date they were first ingested, which is only an upper
  bound, so they are marked estimated.
"""
import argparse
import collections
import difflib
import hashlib
import io
import itertools
import json
import math
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "log.txt"
STORE = ROOT / "tools" / "cube_stats.json"
TARGET = ROOT / "last_cubes_target.json"
STORE_VERSION = 1

# Sessions logged before the ITEM picker (2026-10-19) were all this slot.
ITEM_BEFORE_PICKER = "secondary"

# A refused press counted as a roll until v1.37. Measured duplicate rate, as
# back-to-back identical card text: 47.6% on v1.35, 48.2% on v1.36, and 0.1% on
# v1.38 over 1149 reads. So pre-v1.37 roll counts are inflated by about 48%.
# v1.33 - v1.36 counted a refused press as a roll. Measured back-to-back
# duplicate card rate: v1.33/34 tiny samples, v1.35 48.1%, v1.36 48.3%,
# v1.37 0.0%, v1.38 0.1%. Versions BELOW v1.33 are honest too (v1.26 5.6%,
# v1.29 1.7% - chance repeats), so this is a window, not a cutoff.
DOUBLE_COUNT_FROM = (1, 33)
DOUBLE_COUNT_UNTIL = (1, 36)
DOUBLE_COUNT_RATE = 0.48

START = re.compile(r"--- Start: (?P<app>\S+?), (?P<cube>\w+) cube, (?P<cards>\d+) card\(s\)"
                   r"(?:, (?P<date>\d{4}-\d{2}-\d{2}))?")
ROLL = re.compile(r"Roll (?P<n>\d+): (?P<verdict>.*)")
CARD = re.compile(r"#1 \[(?P<marks>[a-z-]*)\] (?P<rest>.*)")
VALUE = re.compile(r"^(?P<stat>.*?)\s(?P<val>[+-]\d+ ?(?:%|[A-Za-z]+)?)$")
APP_VER = re.compile(r"v(\d+)\.(\d+)")

LINE_POOLS = (("LINE 1 - legendary on every roll", "line1"),
              ("LINE 2 or 3 when it IS legendary", "leg23"),
              ("LINE 2 or 3 when it is unique", "uni23"))


# ---------------------------------------------------------------- the store ---
def load_store():
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault("store_version", STORE_VERSION)
    data.setdefault("cursor", {})
    data.setdefault("sessions", [])
    return data


def save_store(store):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(store, indent=1), encoding="utf-8")


def fingerprint(path):
    """Enough of the head to notice the log was replaced rather than appended."""
    with io.open(path, "rb") as fh:
        return hashlib.sha1(fh.read(4096)).hexdigest()


# -------------------------------------------------------------- the parsing ---
def parse_card(rest):
    """'STR +12%, All Stats +6%, DEX +9%' -> the three lines as written."""
    if rest == "nothing read":
        return []
    out = []
    for part in rest.split(", "):
        m = VALUE.match(part)
        out.append("%s %s" % (m.group("stat"), m.group("val")) if m else part.rstrip("?"))
    return out


def blank_session(clock, app, cube, cards, date):
    return {"date": date, "estimated_date": date is None, "time": clock,
            "app": app, "cube": cube, "cards": int(cards), "item": None, "goal": None,
            "rolls": 0, "read": 0, "skipped": 0, "stop": None,
            "patterns": collections.Counter(), "line1": collections.Counter(),
            "leg23": collections.Counter(), "uni23": collections.Counter()}


def plain(session):
    """Counters -> dicts, so the session can be written as JSON."""
    for key in ("patterns", "line1", "leg23", "uni23"):
        session[key] = dict(session[key])
    return session


def read_sessions(text):
    """Every session in *text*, plus the offset where an unfinished one began.

    A session with no 'Stopped after' line may still be running, so its bytes
    are deliberately left unread: next time it is picked up whole, rather than
    counted once in halves.
    """
    done, live, live_at, offset = [], None, None, 0
    for raw in text.splitlines(keepends=True):
        started_at, offset = offset, offset + len(raw.encode("utf-8"))
        raw = re.sub(r"^\d{4}-\d\d-\d\d ", "", raw)     # lines since v1.44 also carry the date
        body = raw[10:].replace("[Cubes] ", "", 1).strip()
        head = START.search(body)
        if head:
            if live is not None:
                done.append(plain(live))     # no stop line: closed by the next start
            live = blank_session(raw[:8], head.group("app"), head.group("cube"),
                                 head.group("cards"), head.group("date"))
            live_at = started_at
            continue
        if live is None:
            continue
        if body.startswith("item:"):
            live["item"] = body[5:].strip()
        elif body.startswith("goal:"):
            live["goal"] = body[5:].strip()
        elif body.startswith("Stopped after"):
            live["stop"] = body
            done.append(plain(live))
            live, live_at = None, None
        elif ROLL.match(body):
            live["rolls"] += 1
            if "skipped the read" in ROLL.match(body).group("verdict"):
                live["skipped"] += 1
                live["patterns"]["skipped"] += 1
            else:
                live["read"] += 1
        else:
            card = CARD.match(body)
            if not card or len(card.group("marks")) != 3:
                continue
            lines = parse_card(card.group("rest"))
            if len(lines) != 3:
                continue
            marks = card.group("marks")
            live["patterns"][marks] += 1
            live["line1"][lines[0]] += 1
            for i in (1, 2):
                live["leg23" if marks[i] == "l" else "uni23"][lines[i]] += 1
    return done, live_at


def session_key(s):
    return (s["date"], s["time"], s["app"], s["rolls"])


def ingest(store, rebuild=False):
    if not LOG.exists():
        print("no log at %s" % LOG)
        return 0
    fp, size = fingerprint(LOG), LOG.stat().st_size
    cursor = store["cursor"]
    start = cursor.get("bytes", 0)
    if rebuild or (cursor and cursor.get("fingerprint") != fp) or start > size:
        if cursor and not rebuild:
            print("log.txt was replaced or truncated - reading it from the start")
        start, store["sessions"] = 0, []
    with io.open(LOG, "rb") as fh:
        fh.seek(start)
        text = fh.read().decode("utf-8", errors="replace")

    sessions, live_at = read_sessions(text)
    today = time.strftime("%Y-%m-%d")
    seen = {session_key(s) for s in store["sessions"]}
    added = 0
    for s in sessions:
        if s["date"] is None:
            s["date"] = today                      # an upper bound; flagged estimated
        if s["item"] is None:
            s["item"] = ITEM_BEFORE_PICKER
            s["assumed_item"] = True
        if session_key(s) in seen:                 # re-read of something already stored
            continue
        seen.add(session_key(s))
        store["sessions"].append(s)
        added += 1
    read_to = start + (live_at if live_at is not None else len(text.encode("utf-8")))
    store["cursor"] = {"bytes": read_to, "fingerprint": fp, "last_ingest": today}
    store["sessions"].sort(key=lambda s: (s["date"], s["time"]))
    print("ingested %d new session(s); %d in the store" % (added, len(store["sessions"])))
    if live_at is not None:
        print("the last session has no stop line yet - left for next time")
    return added


# --------------------------------------------------------------- the report ---
def fold(counter):
    """Fold rare OCR misspellings onto the line they came from.

    Frequency decides first. 'Boss Damage +35%' and '+40%' are 80% alike as
    strings but are two different lines of the pool, and both are common - so
    any spelling worth 1% or more of the pool is real, and similarity only
    places what is left over.
    """
    merged = collections.Counter()
    for name, n in counter.items():
        merged[re.sub(r"^\?\?\s*", "", name)] += n
    total = sum(merged.values()) or 1
    real = {k for k, n in merged.items() if n / total >= 0.01}
    out = collections.Counter()
    for name, n in merged.items():
        if name in real or not real:
            out[name] += n
            continue
        near = max(real, key=lambda c: difflib.SequenceMatcher(None, c, name).ratio())
        ratio = difflib.SequenceMatcher(None, near, name).ratio()
        out[near if ratio > 0.85 else name] += n
    return collections.Counter({k: v for k, v in out.items() if v / total >= 0.01})


def pooled(sessions, key):
    merged = collections.Counter()
    for s in sessions:
        merged.update(s[key])
    return merged


def real_cubes(sessions):
    """Logged rolls corrected for the v1.33-v1.36 double count."""
    inflated = honest = 0
    for s in sessions:
        m = APP_VER.match(s["app"] or "")
        ver = (int(m.group(1)), int(m.group(2))) if m else None
        if ver and DOUBLE_COUNT_FROM <= ver <= DOUBLE_COUNT_UNTIL:
            inflated += s["rolls"]
        else:
            honest += s["rolls"]
    return inflated, honest, round(inflated * (1 - DOUBLE_COUNT_RATE)) + honest


def table(title, counter, width=34):
    total = sum(counter.values())
    print("\n%s  -  %d lines, %d distinct" % (title, total, len(counter)))
    for name, n in counter.most_common():
        print("   %-*s %7.2f%%   1 in %5.1f   (%d)"
              % (width, name[:width], 100 * n / total, total / n, n))


def rate(label, n, total):
    if not n:
        return "   %-18s none seen" % label
    return "   %-18s %7.2f%%   1 in %6.1f" % (label, 100 * n / total, total / n)


def list_sessions(sessions):
    for s in sessions:
        print("%s %s  %-8s %-10s %-22s rolls %5d  read %5d  %s"
              % (s["date"], s["time"], s["cube"], s["item"], s["app"],
                 s["rolls"], s["read"], (s["goal"] or "(none logged)")[:50]))


def group_of(session):
    return (session["cube"], session["item"], session["goal"] or "(none logged)")


def narrow_to_latest_group(sessions):
    """One cube type, one item slot, one goal - whichever the latest used.

    Pooling any two of those three averages two different potential pools into
    a number that describes neither:

    * Glowing and Bright are different cubes with their own line odds and
      their own tier odds. The same roll is not worth the same from each.
    * Every item slot has its own pool - a hat cannot roll Attack Power %.
    * The goal gets changed when the item does, so a goal boundary is usually
      an item boundary as well.

    Seen for real: pooling the whole log put '+13%' lines in the same column
    as '+12%' ones, which cannot have come from one item.
    """
    groups = collections.Counter(group_of(s) for s in sessions)
    if len(groups) < 2:
        return sessions
    latest = group_of(sessions[-1])
    print("%d cube/item/goal groups in range - reporting the latest only." % len(groups))
    print("--all pools them anyway; --cube, --item or --goal picks another:")
    for (cube, item, goal), n in groups.most_common():
        print("   x%-4d %-8s %-10s %s" % (n, cube, item, goal[:52]))
    print()
    return [s for s in sessions if group_of(s) == latest]


def report(store, cube=None, item=None, goal_contains=None, every=False):
    sessions = store["sessions"]
    if cube:
        sessions = [s for s in sessions if (s["cube"] or "").lower() == cube.lower()]
    if item:
        sessions = [s for s in sessions if s["item"] == item]
    if goal_contains:
        sessions = [s for s in sessions if goal_contains in (s["goal"] or "")]
    if not sessions:
        print("no sessions match")
        return
    if not every:
        sessions = narrow_to_latest_group(sessions)

    by_cube = collections.Counter(s["cube"] for s in sessions)
    by_item = collections.Counter(s["item"] for s in sessions)
    assumed = sum(1 for s in sessions if s.get("assumed_item"))
    estimated = sum(1 for s in sessions if s.get("estimated_date"))
    inflated, honest, real = real_cubes(sessions)

    print("=" * 72)
    print("%d session(s), %s to %s" % (len(sessions), sessions[0]["date"], sessions[-1]["date"]))
    print("cube: " + ", ".join("%s x%d" % kv for kv in by_cube.most_common()))
    if len(by_cube) > 1:
        print("  WARNING: two cube types pooled - their odds differ, so these numbers")
        print("  describe neither. Pass --cube bright or --cube glowing.")
    print("item slots: " + ", ".join("%s x%d" % kv for kv in by_item.most_common()))
    if assumed:
        print("  (%d predate the ITEM picker, filed as %s)" % (assumed, ITEM_BEFORE_PICKER))
    if estimated:
        print("  (%d predate the logged date - their date is an upper bound)" % estimated)
    print("\nrolls logged %d   read %d   skipped on tier icons %d"
          % (sum(s["rolls"] for s in sessions), sum(s["read"] for s in sessions),
             sum(s["skipped"] for s in sessions)))
    print("real cubes ~%d  (%d logged on v1.33-v1.36 at ~%d%% double-counted, + %d honest)"
          % (real, inflated, 100 * DOUBLE_COUNT_RATE, honest))

    patterns = pooled(sessions, "patterns")
    total = sum(patterns.values())
    print("\ntier patterns over all %d rolls  (l=legendary u=unique; 'skipped' is" % total)
    print("1 legendary + 2 lower, never read - the skip gate needs 2 legendary)")
    for name, n in patterns.most_common():
        print("   %-9s %8.3f%%   (%d)" % (name, 100 * n / total, n))
    read_pat = [(p, n) for p, n in patterns.items() if len(p) == 3]
    print(rate("line 2 legendary", sum(c for p, c in read_pat if p[1] == "l"), total))
    print(rate("line 3 legendary", sum(c for p, c in read_pat if p[2] == "l"), total))
    print(rate("both 2 and 3", sum(c for p, c in read_pat if p[1] == "l" and p[2] == "l"), total))

    for title, key in LINE_POOLS:
        merged = pooled(sessions, key)
        if merged:
            table(title, fold(merged))

    odds(sessions, patterns)


def load_goal():
    """The saved goal, rebuilt with the app's own classes."""
    sys.path.insert(0, str(ROOT))
    import main
    saved = json.loads(TARGET.read_text(encoding="utf-8"))
    main.ALL_STATS_COUNTS = bool(saved.get("all_stats", True))

    def build(g):
        made = (main.ComboGoal(g["combo"], int(g.get("count", 3))) if g["kind"] == "combo"
                else main.PerStatGoal(g["needs"]))
        need = str(g.get("legendary", 0))
        return main.LegendaryOnly(made, int(need)) if need in ("2", "3") else made

    goals = [build(g) for g in saved["goals"]]
    if len(goals) == 1:
        return goals[0]
    return main.CombinedGoal(goals, [str(g.get("join", "any")) for g in saved["goals"][1:]])


def weighted(counter):
    """A fold of *counter* as [((stat, value), share)], shares summing to 1."""
    folded = fold(counter)
    total = sum(folded.values()) or 1
    out = []
    for name, n in folded.most_common():
        m = VALUE.match(name)
        out.append(((m.group("stat"), m.group("val")) if m else (name, None), n / total))
    return out


def odds(sessions, patterns):
    """Chance per cube of the saved goal, from the measured pools.

    Enumerates every card the pools can make and asks main.py's own matcher,
    so it tests the shipped goal classes rather than a second reading of them.
    """
    try:
        target = load_goal()
    except Exception as exc:
        print("\n(no odds: %s)" % exc)
        return

    # Line 1 and a legendary slot 2/3 draw from the same pool, so pool them.
    leg = weighted(pooled(sessions, "line1") + pooled(sessions, "leg23"))
    uni = weighted(pooled(sessions, "uni23"))
    if not leg or not uni:
        print("\n(no odds: not enough read rolls yet)")
        return

    # 'skipped' means 1 legendary + 2 lower, never read. Treated as two uniques,
    # which is what a non-legendary line turns out to be whenever one IS read.
    pat = collections.Counter()
    for p, n in patterns.items():
        pat["luu" if p == "skipped" else p] += n
    total = sum(pat.values())

    hits = collections.Counter()
    for marks, count in pat.items():
        if len(marks) != 3 or any(c not in "lu" for c in marks):
            continue                                   # a tier misread; too rare to model
        pools = [leg if c == "l" else uni for c in marks]
        tiers = ["legendary" if c == "l" else "unique" for c in marks]
        share = count / total
        for (a, wa), (b, wb), (c, wc) in itertools.product(*pools):
            why = target.check([a, b, c], tiers)
            if why:
                hits[why.split("  OR  ")[0]] += share * wa * wb * wc

    p = sum(hits.values())
    print("\n" + "=" * 72)
    print("goal: %s" % target.describe())
    if not p:
        print("no card these pools can make satisfies it")
        return
    print("\nchance per cube: %.3e   ->   1 in %s" % (p, format(round(1 / p), ",")))
    for q in (0.25, 0.5, 0.9, 0.99):
        print("   %4.0f%% chance by %9s cubes"
              % (100 * q, format(round(math.log(1 - q) / math.log(1 - p)), ",")))
    print("\nwhich condition fires:")
    for why, w in hits.most_common():
        print("   %6.1f%% of hits   1 in %9s   %s" % (100 * w / p, format(round(1 / w), ","), why))


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ingest", action="store_true", help="read the new part of the log, no report")
    ap.add_argument("--report", action="store_true", help="report from the store, read nothing")
    ap.add_argument("--rebuild", action="store_true", help="forget the cursor, re-read the whole log")
    ap.add_argument("--cube", help="only this cube type (bright / glowing)")
    ap.add_argument("--item", help="only this item slot")
    ap.add_argument("--goal", help="only sessions whose goal contains this text")
    ap.add_argument("--all", action="store_true",
                    help="pool every cube/item/goal group instead of the latest one")
    ap.add_argument("--list", action="store_true", help="one line per stored session, then stop")
    args = ap.parse_args(argv)

    store = load_store()
    if not args.report:
        ingest(store, rebuild=args.rebuild)
        save_store(store)
        print("store: %s" % STORE)
    if args.list:
        list_sessions(store["sessions"])
        return 0
    if not args.ingest:
        report(store, cube=args.cube, item=args.item, goal_contains=args.goal, every=args.all)
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
