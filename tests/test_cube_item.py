"""The item slot has to reach the log, because that is all it is for.

It changes nothing in a run. Its whole value is that a finished session says
which slot it rolled, so the rolls can be split by slot afterwards - every slot
has its own potential pool, and a table built from two slots' rolls describes
neither. A picker that quietly fails to log is therefore the one way this
feature can be broken while still looking fine on screen.
"""
import unittest
from types import SimpleNamespace

from main import CUBE_ITEM_NONE, CUBE_ITEMS, CubesApp

# The slots the user asked for, in the order they asked for them.
REQUESTED = ("accessories", "belt", "weapon", "secondary", "emblem", "heart",
             "boots", "gloves", "cape", "hat", "top", "bottom")


def run_on(item):
    """A run ready to log, rolling *item*."""
    written = []
    return written, SimpleNamespace(
        _log=written.append,
        _describe=lambda target: "a goal",
        _item_var=SimpleNamespace(get=lambda: item),
        _all_stats_var=SimpleNamespace(get=lambda: True),
        profile=SimpleNamespace(name="Bright"),
        region=(10, 20, 30, 40), panels=[(10, 20, 30, 40)], count_box=None,
        target=SimpleNamespace(describe=lambda: "a goal"),
    )


class CubeItemTest(unittest.TestCase):
    def test_every_slot_asked_for_is_offered(self):
        self.assertEqual(REQUESTED, CUBE_ITEMS[1:])

    def test_the_list_opens_unset_so_labelling_stays_optional(self):
        self.assertEqual(CUBE_ITEM_NONE, CUBE_ITEMS[0])

    def test_no_slot_is_listed_twice(self):
        self.assertEqual(len(CUBE_ITEMS), len(set(CUBE_ITEMS)))

    def test_the_slot_is_written_to_the_log(self):
        written, run = run_on("secondary")
        CubesApp._log_run_details(run)
        self.assertIn("    item: secondary", written)

    def test_an_unlabelled_run_still_logs_its_slot_line(self):
        """Silence would read as an old version rather than an unset picker."""
        written, run = run_on(CUBE_ITEM_NONE)
        CubesApp._log_run_details(run)
        self.assertIn("    item: not set", written)


if __name__ == "__main__":
    unittest.main()
