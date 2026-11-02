"""The screener's one job beyond listing stocks: refusing to present a bad rule as a find."""
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


scan = _load("nse-scan", "scan")


def record(**kw):
    base = {"setup": "x", "hold_days": 10, "stop_percent": 5.0, "trades": 500,
            "win_rate": 45.0, "average": 2.0, "median": 1.0, "baseline": 0.5, "edge": 1.5,
            "window": "a to b", "measured_on": "2026-10-23 12:00"}
    base.update(kw)
    return base


class WhatIsWorthActingOn(unittest.TestCase):
    def test_a_rule_with_a_real_record_is_worth_acting_on(self):
        note, ok = scan.record_line(record(), "x")
        self.assertTrue(ok)
        self.assertIn("edge +1.50pp", note)

    def test_a_rule_that_lost_to_random_is_called_out(self):
        """The whole point. A screener that hides this is a screener that misleads."""
        note, ok = scan.record_line(record(edge=-0.4, average=-2.3), "x")
        self.assertFalse(ok)
        self.assertIn("LOST TO BUYING AT RANDOM", note)

    def test_a_rule_with_too_few_trades_is_not_evidence(self):
        """57 trades is what the shipped delivery-spike rule has, and it is not enough.

        The threshold is 100 rather than the usual 30: thirty is the rule of thumb for a mean
        behaving normally, and far too few to claim a win rate from.
        """
        note, ok = scan.record_line(record(trades=57), "x")
        self.assertFalse(ok)
        self.assertIn("not enough to claim a win rate", note)

    def test_the_threshold_is_a_hundred_not_thirty(self):
        self.assertEqual(scan.MEANINGFUL_TRADES, 100)
        _note, ok = scan.record_line(record(trades=99), "x")
        self.assertFalse(ok)
        _note, ok = scan.record_line(record(trades=101), "x")
        self.assertTrue(ok)

    def test_a_median_at_the_stop_is_called_out(self):
        """Measured on all four shipped setups: the median return equals the stop exactly.

        More than half the trades lose and the average is carried by a few outliers, which is a
        fragile shape however good the average looks.
        """
        note, ok = scan.record_line(record(median=-5.0, stop_percent=5.0), "x")
        self.assertFalse(ok)
        self.assertIn("median trade hit the stop", note)

    def test_an_unmeasured_rule_says_so_and_names_the_command(self):
        note, ok = scan.record_line(None, "delivery-spike")
        self.assertFalse(ok)
        self.assertIn("NOT been measured", note)
        self.assertIn("--setup delivery-spike", note)

    def test_a_rule_measured_with_no_trades_counts_as_unmeasured(self):
        note, ok = scan.record_line(record(trades=0), "x")
        self.assertFalse(ok)
        self.assertIn("NOT been measured", note)


class SharedDefinitions(unittest.TestCase):
    def test_the_screener_does_not_keep_its_own_copy_of_the_setups(self):
        """A rule screened on one definition and measured on another is worse than no measurement.

        Identity against a freshly loaded backtest module would always fail, since loading a file
        twice makes new function objects. What matters is that scan reached into the replay for its
        definitions rather than defining any itself.
        """
        self.assertIs(scan.SETUPS, scan._bt.SETUPS)
        bt = _load("nse-backtest", "backtest")
        self.assertEqual(sorted(scan.SETUPS), sorted(bt.SETUPS))

    def test_the_turnover_floor_is_shared_too(self):
        bt = _load("nse-backtest", "backtest")
        self.assertEqual(scan.MIN_TURNOVER_LACS, bt.MIN_TURNOVER_LACS)


class Firing(unittest.TestCase):
    def _store(self, bars):
        tmp = tempfile.mkdtemp()
        store = Prices(Path(tmp) / "p.sqlite")
        for b in bars:
            store.put_day(b["traded_on"], [b], "test")
        return store

    def _bar(self, day, close, delivery=30.0, turnover=5000.0):
        return {"symbol": "TEST", "traded_on": day, "open": close, "high": close, "low": close,
                "close": close, "previous_close": close, "volume": 1000.0,
                "turnover": turnover, "delivered": 500.0, "delivery_percent": delivery}

    def test_an_illiquid_stock_is_never_a_candidate(self):
        days = [f"2026-01-{n:02d}" for n in range(1, 26)]
        bars = [self._bar(d, 100.0, delivery=20.0, turnover=1.0) for d in days]
        bars[-1] = self._bar(days[-1], 101.0, delivery=99.0, turnover=1.0)  # spike, but untradeable
        store = self._store(bars)
        try:
            found = scan.firing(store, "delivery-spike", 20)
        finally:
            store.close()
        self.assertEqual(found, [])

    def test_a_past_day_can_be_scanned(self):
        days = [f"2026-01-{n:02d}" for n in range(1, 26)]
        bars = [self._bar(d, 100.0) for d in days]
        store = self._store(bars)
        try:
            found = scan.firing(store, "breakout", 20, on=days[22])
            self.assertIsInstance(found, list)
        finally:
            store.close()

    def test_a_day_outside_the_cache_is_refused_not_guessed(self):
        days = [f"2026-01-{n:02d}" for n in range(1, 26)]
        store = self._store([self._bar(d, 100.0) for d in days])
        try:
            with self.assertRaises(SystemExit):
                scan.firing(store, "breakout", 20, on="2099-01-01")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
