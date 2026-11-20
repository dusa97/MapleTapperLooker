"""Ingesting the log twice must not count a roll twice.

tools/cube_stats.py exists so the log is read once and never again. That makes
the cursor the only thing holding the numbers up: if it lands in the middle of
a session, the next run re-reads the first half and the roll count silently
drifts upward - and a statistic that is quietly 5% high is worse than no
statistic, because nothing about it looks wrong.

The case that catches it is a session still running when the ingest happens.
Its bytes have to be left alone and picked up whole next time.
"""
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import cube_stats


def session(clock, date, item, rolls, finished=True):
    """A session as the app writes one: header, then rolls, then the stop."""
    out = [f"{clock}  [Cubes] --- Start: v1.40.0, Bright cube, 1 card(s), {date}"]
    if item:
        out.append(f"{clock}  [Cubes]     item: {item}")
    out.append(f"{clock}  [Cubes]     goal: 3 of 3 lines are Attack Power")
    for n in range(1, rolls + 1):
        if n % 2:
            out.append(f"{clock}  [Cubes] Roll {n}: no match yet, ink 0.1")
            out.append(f"{clock}  [Cubes]     #1 [llu] Attack Power +12%, STR +12%, DEX +9%")
        else:
            out.append(f"{clock}  [Cubes] Roll {n}: skipped the read (tiers cannot match)")
    if finished:
        out.append(f"{clock}  [Cubes] Stopped after {rolls} roll(s): out of cubes.")
    return "\n".join(out) + "\n"


FIRST = session("12:00:00", "2026-10-19", "weapon", 4)
SECOND = session("13:00:00", "2026-10-19", "hat", 2)
UNFINISHED = session("14:00:00", "2026-10-20", "cape", 3, finished=False)
TAIL = "14:00:00  [Cubes] Stopped after 3 roll(s): out of cubes.\n"
NO_ITEM_LINE = session("15:00:00", "2026-10-19", None, 2)


class Ingesting:
    """A throwaway log and store, so the real ones are never touched."""

    def __enter__(self):
        self._dir = TemporaryDirectory()
        base = Path(self._dir.name)
        self._saved = cube_stats.LOG, cube_stats.STORE
        cube_stats.LOG = base / "log.txt"
        cube_stats.STORE = base / "store.json"
        cube_stats.LOG.write_text("", encoding="utf-8")
        self.store = cube_stats.load_store()
        return self

    def __exit__(self, *_):
        cube_stats.LOG, cube_stats.STORE = self._saved
        self._dir.cleanup()

    def append(self, text):
        with open(cube_stats.LOG, "a", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return cube_stats.ingest(self.store)

    def totals(self):
        return (len(self.store["sessions"]),
                sum(s["rolls"] for s in self.store["sessions"]),
                sum(s["read"] for s in self.store["sessions"]))


class CubeStatsTest(unittest.TestCase):
    def test_reading_the_log_in_pieces_matches_reading_it_at_once(self):
        with Ingesting() as one:
            one.append(FIRST + SECOND)
            at_once = one.totals()
        with Ingesting() as split:
            split.append(FIRST)
            split.append(SECOND)
            self.assertEqual(at_once, split.totals())
        self.assertEqual((2, 6, 3), at_once)      # 4 + 2 rolls, every other one read

    def test_ingesting_again_with_nothing_new_changes_nothing(self):
        with Ingesting() as run:
            run.append(FIRST)
            before = run.totals()
            self.assertEqual(0, run.append(""))
            self.assertEqual(0, run.append(""))
            self.assertEqual(before, run.totals())

    def test_a_session_still_running_is_left_for_next_time(self):
        """The one that catches a cursor landing mid-session."""
        with Ingesting() as run:
            run.append(FIRST + UNFINISHED)
            self.assertEqual((1, 4, 2), run.totals())    # only the finished one
            run.append(TAIL)
            self.assertEqual((2, 7, 4), run.totals())    # 4 + 3 rolls, each counted once

    def test_a_truncated_log_is_re_read_from_the_start(self):
        with Ingesting() as run:
            run.append(FIRST + SECOND)
            cube_stats.LOG.write_text(SECOND, encoding="utf-8")   # cleared and restarted
            run.append("")
            self.assertEqual((1, 2, 1), run.totals())

    def test_the_item_slot_and_date_come_off_the_session_header(self):
        with Ingesting() as run:
            run.append(FIRST + SECOND)
            slots = {s["item"]: s for s in run.store["sessions"]}
            self.assertEqual({"weapon", "hat"}, set(slots))
            self.assertEqual("2026-10-19", slots["hat"]["date"])
            self.assertFalse(slots["hat"]["estimated_date"])

    def test_a_session_with_no_item_line_is_filed_as_the_old_slot(self):
        """Sessions predating the ITEM picker were all a secondary."""
        with Ingesting() as run:
            run.append(NO_ITEM_LINE)
            stored = run.store["sessions"][0]
            self.assertEqual(cube_stats.ITEM_BEFORE_PICKER, stored["item"])
            self.assertTrue(stored["assumed_item"])

    def test_the_card_lines_land_in_the_right_pools(self):
        with Ingesting() as run:
            run.append(FIRST)                       # two [llu] cards
            s = run.store["sessions"][0]
            self.assertEqual({"Attack Power +12%": 2}, s["line1"])
            self.assertEqual({"STR +12%": 2}, s["leg23"])
            self.assertEqual({"DEX +9%": 2}, s["uni23"])
            self.assertEqual({"llu": 2, "skipped": 2}, s["patterns"])

    def test_the_store_is_json(self):
        with Ingesting() as run:
            run.append(FIRST)
            cube_stats.save_store(run.store)
            json.loads(cube_stats.STORE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
