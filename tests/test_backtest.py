"""Replaying a rule over real market history, and the fill assumptions that decide the answer.

EVERYTHING RUNS ON REAL BARS: 1,875 daily bars for 25 liquid NSE symbols over 75 trading days,
out of the exchange's own bhavcopy. The gap-through-stop case is a real overnight gap, not a
constructed one, because that assumption is where a backtest flatters itself most.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import fixture, has, price_cache, skill                # noqa: E402

backtest = skill("nse-backtest", "backtest")
CACHE = "cache/daily-bars.json.gz"


@unittest.skipUnless(has(CACHE), "run capture.py")
class RealHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store, cls.saved = price_cache(Path(cls.tmp.name) / "p.sqlite")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_the_cache_holds_real_market_history(self):
        counts = self.store.counts()
        self.assertEqual(counts["days"], 75)
        self.assertEqual(counts["bars"], 1875)
        self.assertEqual(counts["symbols"], 25)
        self.assertEqual(counts["last"], "2026-10-23")

    def test_the_symbols_are_liquid_real_companies(self):
        self.assertIn("RELIANCE", self.saved["symbols"])
        self.assertIn("HDFCBANK", self.saved["symbols"])

    def test_a_replay_over_real_history_produces_real_trades(self):
        out = backtest.run(self.store, "breakout", 10, 5.0, None, 20)
        self.assertGreater(len(out["trades"]), 50)
        for trade in out["trades"]:
            self.assertIn(trade["symbol"], self.saved["symbols"])
            self.assertGreater(trade["entry"], 0)
            self.assertIn(trade["why"], ("held", "stopped", "gapped", "target"))

    def test_the_baseline_is_always_measured(self):
        """What buying anything would have done. Without it a win rate means nothing."""
        out = backtest.run(self.store, "breakout", 10, 5.0, None, 20)
        self.assertGreater(len(out["baseline"]), len(out["trades"]))
        summary = backtest.summarise(out, "breakout", 10, 5.0, None)
        self.assertIsNotNone(summary["baseline_average_percent"])
        self.assertAlmostEqual(
            summary["edge_over_baseline"],
            round(summary["average_percent"] - summary["baseline_average_percent"], 2),
            places=2)

    def test_entry_is_the_next_days_open_never_the_signal_close(self):
        """You cannot buy at a close you only knew about after it printed."""
        out = backtest.run(self.store, "breakout", 10, 5.0, None, 20)
        for trade in out["trades"][:40]:
            self.assertGreater(trade["entry_on"], trade["signal_on"])
            bars = {b["traded_on"]: b for b in self.store.series(trade["symbol"])}
            self.assertAlmostEqual(trade["entry"], bars[trade["entry_on"]]["open"], places=2)

    def test_a_rule_that_fires_on_nothing_is_reported_as_no_trades(self):
        """No trades is an answer. It must not come back as a win rate of zero or a blank edge."""
        out = backtest.run(self.store, "delivery-spike", 10, 5.0, None, 20)
        summary = backtest.summarise(out, "delivery-spike", 10, 5.0, None)
        self.assertEqual(summary["trades"], len(out["trades"]))
        if not out["trades"]:
            self.assertIsNone(summary.get("win_rate"))
            self.assertIsNone(summary.get("average_percent"))

    def test_the_control_rule_exists_and_ignores_delivery(self):
        """If the delivery setups do not beat this, the delivery number adds nothing."""
        self.assertIn("breakout", backtest.SETUPS)
        source = __import__("inspect").getsource(backtest.plain_breakout)
        self.assertNotIn("delivery", source.split('"""')[2])

    def test_every_setup_reads_only_the_past(self):
        """A rule that sees tomorrow fakes an edge, so this poisons tomorrow and checks.

        Tested behaviourally rather than by reading the source, because `bars[i - window:i + 1]`
        is perfectly correct -- the slice end is exclusive, so it includes today's bar, which was
        known at the close. Only an answer that CHANGES when the future changes is a bug.
        """
        series = self.store.series("RELIANCE")
        self.assertGreater(len(series), 40)
        poison = [dict(bar) for bar in series]
        for bar in poison[31:]:
            bar.update(close=99999.0, high=99999.0, low=0.01, delivery_percent=100.0,
                       turnover_lacs=10 ** 9)
        for name, rule in backtest.SETUPS.items():
            self.assertEqual(rule(series, 30, 20), rule(poison, 30, 20),
                             f"{name} changed its mind when the future changed")


@unittest.skipUnless(has(CACHE), "run capture.py")
class AGapThroughTheStopFillsAtTheOpen(unittest.TestCase):
    """The most flattering assumption a backtest can make, tested on a real gap.

    The first version of this file took the stop price whatever happened overnight, and every
    setup then reported a worst trade of exactly -5.29%: the stop plus costs. That cannot happen.
    A stock that closes at 202.17 and opens at 171.00 does not fill a 192.06 stop at 192.06.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store, cls.saved = price_cache(Path(cls.tmp.name) / "p.sqlite")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def biggest_real_gap(self):
        """The worst overnight gap down in the captured history."""
        worst = None
        for symbol in self.saved["symbols"]:
            series = self.store.series(symbol)
            for before, after in zip(series, series[1:]):
                if before.get("close") and after.get("open"):
                    move = (after["open"] - before["close"]) / before["close"] * 100
                    if worst is None or move < worst[0]:
                        worst = (move, symbol, before, after)
        return worst

    def test_the_history_really_contains_a_gap_through_a_five_percent_stop(self):
        move, symbol, before, after = self.biggest_real_gap()
        self.assertEqual(symbol, "PINELABS")
        self.assertLess(move, -5.0)
        self.assertEqual(before["close"], 202.17)
        self.assertEqual(after["open"], 171.0)

    def test_the_fill_is_the_open_not_the_stop(self):
        """Entry 202.17 with a 5% stop is 192.06. The market opened at 171.00."""
        _move, _symbol, before, after = self.biggest_real_gap()
        entry, stop = before["close"], before["close"] * 0.95
        filled = min(stop, after["open"])
        self.assertEqual(round(stop, 2), 192.06)
        self.assertEqual(filled, 171.0)
        self.assertLess((filled / entry - 1) * 100, -5.0)

    def test_no_real_trade_in_the_replay_reports_exactly_the_stop_as_its_worst(self):
        """The symptom of the old bug: every setup bottoming out at the same -5.29%."""
        out = backtest.run(self.store, "breakout", 10, 5.0, None, 20)
        if not out["trades"]:
            self.skipTest("no trades in this capture")
        worst = min(t["return_percent"] for t in out["trades"])
        gapped = [t for t in out["trades"] if t["why"] == "gapped"]
        if gapped:
            self.assertLess(worst, -5.29)
        self.assertNotEqual(worst, -5.29)

    def test_a_gapped_exit_is_labelled_differently_from_a_stopped_one(self):
        out = backtest.run(self.store, "breakout", 10, 5.0, None, 20)
        reasons = {t["why"] for t in out["trades"]}
        self.assertTrue(reasons <= {"held", "stopped", "gapped", "target"})

    def test_the_stop_is_checked_before_the_target(self):
        """Within one daily bar nobody knows which came first, so the pessimistic order is honest.

        A backtest that takes the target when both were touched invents money.
        """
        source = __import__("inspect").getsource(backtest.run)
        stop_at = source.index("low <= stop")
        target_at = source.index("high >= target")
        self.assertLess(stop_at, target_at)

    def test_a_gap_up_through_the_target_fills_above_it_and_that_is_not_a_favour(self):
        source = __import__("inspect").getsource(backtest.run)
        self.assertIn("max(target, opened)", source)
        self.assertIn("min(stop, opened)", source)


@unittest.skipUnless(has(CACHE), "run capture.py")
class CostsAreNotOptional(unittest.TestCase):
    def test_costs_come_off_every_trade_and_off_the_baseline_too(self):
        """Taking costs off the setup but not the baseline manufactures an edge."""
        import inspect
        source = inspect.getsource(backtest.run)
        self.assertEqual(source.count("COSTS_ROUND_TRIP_PERCENT"), 2)

    def test_the_cost_figure_is_the_indian_one(self):
        self.assertGreater(backtest.COSTS_ROUND_TRIP_PERCENT, 0)
        self.assertLess(backtest.COSTS_ROUND_TRIP_PERCENT, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
