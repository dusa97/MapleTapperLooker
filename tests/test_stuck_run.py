"""A run the game keeps refusing has to end on its own.

A refused press brings the whole panel back, so the loop's re-press counter -
which only counts "nothing on screen moved" - restarts every single time and
never reaches its limit. On a friend's Reset x3 with fewer than 3 cubes left
that was an unbounded press loop: it clicked until he stopped it by hand, and
the only line in his log was the re-press notice, forever at (1/2).

The bound is time, not a count of refusals. Across every logged session on two
machines - 9,500 rolls, Bright and Glowing, x1 and Reset x3 - the longest gap
between two rolls the app could count is 4 seconds.
"""
import time
import unittest
from types import SimpleNamespace

from main import CUBES_STUCK_SECONDS, CubesApp

LONGEST_REAL_GAP = 4.0        # seconds, measured over both machines' logs


def run_idle_for(seconds):
    """A run whose last counted roll was *seconds* ago."""
    return SimpleNamespace(_last_roll_at=time.perf_counter() - seconds)


class StuckRunTest(unittest.TestCase):
    def test_a_normal_gap_between_rolls_never_ends_the_run(self):
        self.assertFalse(CubesApp._stuck_too_long(run_idle_for(LONGEST_REAL_GAP)))

    def test_a_run_that_only_refuses_ends(self):
        self.assertTrue(CubesApp._stuck_too_long(run_idle_for(CUBES_STUCK_SECONDS + 1)))

    def test_a_roll_just_counted_never_ends_the_run(self):
        self.assertFalse(CubesApp._stuck_too_long(run_idle_for(0)))

    def test_the_cut_clears_the_worst_real_gap_several_times_over(self):
        """The one that would have caught a careless constant: picking the limit
        anywhere near a real gap would stop good runs on a slow client."""
        self.assertGreaterEqual(CUBES_STUCK_SECONDS, 4 * LONGEST_REAL_GAP)
