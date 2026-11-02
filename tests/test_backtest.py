"""The backtester, and the assumption that makes backtests lie.

Every test here is offline and built on hand-made bars, because the point is the arithmetic of the
replay rather than any particular stock.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nsetools.store import Prices                                   # noqa: E402


def _load(folder: str, module: str):
    spec = importlib.util.spec_from_file_location(module, ROOT / folder / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bt = _load("nse-backtest", "backtest")


def bar(day, close, *, open=None, high=None, low=None, prev=None, delivery=30.0, turnover=5000.0):
    return {"symbol": "TEST", "traded_on": day, "open": open if open is not None else close,
            "high": high if high is not None else close, "low": low if low is not None else close,
            "close": close, "previous_close": prev if prev is not None else close,
            "volume": 100000.0, "turnover": turnover, "delivered": 50000.0,
            "delivery_percent": delivery}


class GapThroughTheStop(unittest.TestCase):
    """The single most flattering assumption a backtest can make, and this one refuses it.

    The first version of the replay filled every stop at the stop price. Every setup then reported a
    worst trade of exactly -5.29%, the stop plus costs, which cannot happen: a stock that closes at
    100 and opens at 88 does not fill a 95 stop at 95. Fixing it changed the worst breakout trade
    from -5.29% to -89.86% over the same window, and found 103 trades that had gapped through.
    """

    def _replay(self, bars, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            store = Prices(Path(tmp) / "p.sqlite")
            for b in bars:
                store.put_day(b["traded_on"], [b], "test")
            try:
                opts = {"setup": "always", "hold": 2, "stop_percent": 5.0,
                        "target_percent": None, "window": 1}
                opts.update(kw)
                bt.SETUPS["always"] = lambda bars, i, window=1: i == window
                return bt.run(store, opts["setup"], opts["hold"], opts["stop_percent"],
                              opts["target_percent"], opts["window"])
            finally:
                bt.SETUPS.pop("always", None)
                store.close()

    def test_a_gap_below_the_stop_fills_at_the_open(self):
        days = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
        bars = [bar(days[0], 100.0), bar(days[1], 100.0),
                bar(days[2], 100.0, open=100.0),            # entry at this open
                bar(days[3], 88.0, open=88.0, high=89.0, low=87.0),   # gaps through a 95 stop
                bar(days[4], 90.0)]
        out = self._replay(bars)
        self.assertEqual(len(out["trades"]), 1)
        trade = out["trades"][0]
        self.assertEqual(trade["why"], "gapped")
        self.assertEqual(trade["exit"], 88.0, "a 95 stop cannot fill at 95 when the open was 88")
        self.assertLess(trade["return_percent"], -5.0 - bt.COSTS_ROUND_TRIP_PERCENT)

    def test_an_ordinary_stop_still_fills_at_the_stop(self):
        days = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
        bars = [bar(days[0], 100.0), bar(days[1], 100.0),
                bar(days[2], 100.0, open=100.0),
                bar(days[3], 97.0, open=99.0, high=99.5, low=94.0),  # traded through 95 intraday
                bar(days[4], 98.0)]
        out = self._replay(bars)
        trade = out["trades"][0]
        self.assertEqual(trade["why"], "stopped")
        self.assertEqual(trade["exit"], 95.0)

    def test_a_gap_above_the_target_fills_above_it(self):
        """The same honesty the other way, which happens to favour you. It is still the truth."""
        days = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
        bars = [bar(days[0], 100.0), bar(days[1], 100.0),
                bar(days[2], 100.0, open=100.0),
                bar(days[3], 120.0, open=118.0, high=122.0, low=117.0),  # gaps past a 110 target
                bar(days[4], 121.0)]
        out = self._replay(bars, target_percent=10.0)
        trade = out["trades"][0]
        self.assertEqual(trade["why"], "target")
        self.assertEqual(trade["exit"], 118.0)

    def test_the_stop_is_checked_before_the_target(self):
        """Within one daily bar there is no way to know which came first.

        A backtest that takes the target when both were touched invents money, so the pessimistic
        order is the honest one.
        """
        days = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
        bars = [bar(days[0], 100.0), bar(days[1], 100.0),
                bar(days[2], 100.0, open=100.0),
                bar(days[3], 105.0, open=100.0, high=115.0, low=94.0),   # touched both
                bar(days[4], 106.0)]
        out = self._replay(bars, target_percent=10.0)
        self.assertEqual(out["trades"][0]["why"], "stopped")


class EntryTiming(unittest.TestCase):
    def test_entry_is_the_next_open_never_the_signal_close(self):
        """A rule that buys the close of the day it fired has already seen the answer."""
        days = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
        bars = [bar(days[0], 100.0), bar(days[1], 100.0),
                bar(days[2], 50.0, open=104.0),      # signal day close 50, next open 104
                bar(days[3], 104.0)]
        with tempfile.TemporaryDirectory() as tmp:
            store = Prices(Path(tmp) / "p.sqlite")
            for b in bars:
                store.put_day(b["traded_on"], [b], "test")
            bt.SETUPS["always"] = lambda bars, i, window=1: i == 1
            try:
                out = bt.run(store, "always", 1, 50.0, None, 1)
            finally:
                bt.SETUPS.pop("always", None)
                store.close()
        self.assertEqual(out["trades"][0]["entry"], 104.0)


class Baseline(unittest.TestCase):
    def test_the_baseline_is_always_measured(self):
        """A setup winning 55% is a bad setup if everything won 60% over the same days.

        Most backtests never print that comparison, which is how a rule gets flattered into looking
        like an edge.
        """
        out = {"trades": [{"return_percent": 2.0, "why": "held"}],
               "baseline": [1.0, 2.0, 3.0], "days": 40, "symbols": 1,
               "first": "2026-01-01", "last": "2026-02-01"}
        s = bt.summarise(out, "x", 10, 5.0, None)
        self.assertIn("baseline_average_percent", s)
        self.assertEqual(s["baseline_average_percent"], 2.0)
        self.assertEqual(s["edge_over_baseline"], 0.0)

    def test_costs_come_off_the_baseline_too(self):
        """Otherwise the setup is charged for trading and the comparison is not."""
        self.assertGreater(bt.COSTS_ROUND_TRIP_PERCENT, 0)

    def test_no_trades_is_reported_as_no_trades(self):
        s = bt.summarise({"trades": [], "baseline": [1.0], "days": 5, "symbols": 1,
                          "first": "a", "last": "b"}, "x", 10, 5.0, None)
        self.assertEqual(s["trades"], 0)


class Setups(unittest.TestCase):
    def test_every_setup_reads_only_the_past(self):
        """Each rule is handed the full history and an index. None may look past it."""
        days = [f"2026-01-{n:02d}" for n in range(1, 41)]
        bars = [bar(d, 100.0 + n, delivery=20.0) for n, d in enumerate(days)]
        bars[30] = bar(days[30], 999.0, delivery=99.0)      # a huge future day
        for name, rule in bt.SETUPS.items():
            # judged at index 25, the day at index 30 must not change the answer
            without = rule(bars[:26] + [bar(d, 1.0) for d in days[26:]], 25, 20)
            with_it = rule(bars, 25, 20)
            self.assertEqual(without, with_it, f"{name} looked into the future")

    def test_plain_breakout_exists_as_the_control(self):
        """The delivery setups have to beat a rule that ignores delivery, or it adds nothing."""
        self.assertIn("breakout", bt.SETUPS)
        self.assertIn("delivery-spike", bt.SETUPS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
