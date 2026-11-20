"""The winning roll must write its card to the log like every other roll.

Every losing roll logs its three lines, so a bad read can be checked afterwards.
The hit did not: the roll loop dispatched the hit handler and returned before
reaching the card-log block, and the "Got it after N roll(s)" line carries only
the goal's name. So the 2026-10-20 hit - Attack Power +12%, +12%, +9% after 238
rolls, the single roll out of 11,017 worth keeping - exists nowhere in log.txt.
It survived only because a Discord ping happened to quote it.
"""
import re
import unittest

from main import CubesApp


CARD = re.compile(r"^    #(?P<n>\d) \[(?P<marks>[a-z-]{3})\] (?P<lines>.+)$")


class Loop:
    """Enough of a CubesApp for the hit branch of the roll loop to run.

    _requests is the queue the loop hands work to the UI thread on, so draining
    it is what the real app does a moment later.
    """

    class _Q:
        def __init__(self, sink):
            self.sink = sink

        def put(self, fn):
            self.sink.append(fn)

    def __init__(self):
        self.logged = []
        self.hits = []
        self.requests = []
        self._requests = Loop._Q(self.requests)
        self._describe_cards = CubesApp._describe_cards

    def _log(self, message):
        self.logged.append(message)

    def _on_hit(self, hit):
        self.hits.append(hit)

    def drain(self):
        for fn in self.requests:
            fn()
        self.requests = []


def hit_branch(app, results, hit):
    """The hit branch of CubesApp._cube_loop_body, as the source has it."""
    app._requests.put(lambda cards=CubesApp._describe_cards(results): app._log("    " + cards))
    app._requests.put(lambda hit=hit: app._on_hit(hit))


class HitLogsItsCardTest(unittest.TestCase):
    def setUp(self):
        self.app = Loop()

    def test_the_winning_card_reaches_the_log(self):
        """The real 2026-10-20 hit, replayed."""
        results = [(["legendary", "legendary", "unique"],
                    [("Attack Power", "+12%"), ("Attack Power", "+12%"), ("Attack Power", "+9%")])]
        hit_branch(self.app, results, ("3 of 3 lines are Attack Power", ""))
        self.app.drain()

        self.assertEqual(1, len(self.app.hits), "the hit still has to be handled")
        cards = [m for m in (CARD.match(line) for line in self.app.logged) if m]
        self.assertEqual(1, len(cards), "a hit logs exactly one card line: %r" % self.app.logged)
        self.assertEqual("llu", cards[0].group("marks"))
        self.assertEqual("Attack Power +12%, Attack Power +12%, Attack Power +9%",
                         cards[0].group("lines"))

    def test_the_card_is_logged_before_the_hit_is_handled(self):
        """_on_hit stops the run and fires Discord; either may fail or block.

        The card has to be written by then, so the order inside the queue is
        part of the behaviour, not an accident of how it was written.
        """
        results = [(["legendary", "legendary", "legendary"],
                    [("Magic Attack", "+12%"), ("Magic Attack", "+12%"), ("Boss Damage", "+35%")])]
        hit_branch(self.app, results, ("2 x Magic Attack + 1 x Boss Damage", ""))
        order = []
        self.app._log = lambda m: order.append("log")
        self.app._on_hit = lambda h: order.append("hit")
        self.app.drain()
        self.assertEqual(["log", "hit"], order)

    def test_a_hit_on_the_second_card_names_which_card_won(self):
        results = [(["legendary", "unique", "unique"], [("STR", "+12%")]),
                   (["legendary", "legendary", "unique"],
                    [("Attack Power", "+12%"), ("Attack Power", "+12%"), ("Attack Power", "+9%")])]
        hit_branch(self.app, results, ("3 of 3 lines are Attack Power", ""))
        self.app.drain()
        self.assertIn("#2 [llu] Attack Power +12%, Attack Power +12%, Attack Power +9%",
                      self.app.logged[0])

    def test_the_source_still_logs_the_card_on_the_hit_path(self):
        """The tests above replay the branch, so they pass even if it is removed.

        This one reads CubesApp._cube_loop_body itself: inside
        `if hit is not None:` the card log has to come before _on_hit. Delete
        either and this fails.
        """
        import inspect
        import textwrap
        body = textwrap.dedent(inspect.getsource(CubesApp._cube_loop_body))
        branch = body.split("if hit is not None:", 1)
        self.assertEqual(2, len(branch), "the hit branch moved - re-point this test")
        # Cut at the `return` STATEMENT. A plain split on "return" would cut at
        # the word inside the branch's own comment instead.
        guarded = branch[1].split("\n                return", 1)[0]
        self.assertIn("_describe_cards(results)", guarded,
                      "a hit must log its card; it is the only roll that matters")
        self.assertLess(guarded.index("_describe_cards"), guarded.index("_on_hit"),
                        "log the card before the hit handler can stop the run")

    def test_an_unread_card_says_so_rather_than_logging_nothing(self):
        results = [(["legendary", "legendary", "unique"], [])]
        hit_branch(self.app, results, ("3 of 3 lines are Attack Power", ""))
        self.app.drain()
        self.assertIn("nothing read", self.app.logged[0])


if __name__ == "__main__":
    unittest.main()
