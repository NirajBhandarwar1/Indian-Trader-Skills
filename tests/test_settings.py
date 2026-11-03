"""The shared pieces every skill now carries its own copy of: config, RSI, and the clock edges.

Config and RSI are tested here once rather than in every skill, because the vendoring test proves
each skill's copy is byte for byte this one.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load import ROOT, has, price_cache, skill                   # noqa: E402
from nsetools import clock, settings, setups                     # noqa: E402


class InFolder:
    """Run a block with the working directory somewhere else, and put it back."""

    def __init__(self, where):
        self.where, self.before = where, os.getcwd()

    def __enter__(self):
        os.chdir(self.where)
        return Path(self.where)

    def __exit__(self, *_):
        os.chdir(self.before)


class WhereTheRulesComeFrom(unittest.TestCase):
    def test_no_config_anywhere_means_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, InFolder(tmp):
            got = settings.load(None, {"capital": 1.0})
        self.assertEqual(got, {"capital": 1.0})

    def test_config_json_in_the_working_folder_is_read(self):
        with tempfile.TemporaryDirectory() as tmp, InFolder(tmp) as here:
            (here / "config.json").write_text(json.dumps({"capital": 250000}))
            got = settings.load()
        self.assertEqual(got["capital"], 250000)
        self.assertIn("config.json", got["_from"])

    def test_nse_config_json_is_read_when_there_is_no_other(self):
        with tempfile.TemporaryDirectory() as tmp, InFolder(tmp) as here:
            (here / "nse").mkdir()
            (here / "nse" / "config.json").write_text(json.dumps({"flat_by": "15:10"}))
            self.assertEqual(settings.load()["flat_by"], "15:10")

    def test_a_named_config_that_does_not_exist_is_an_error_not_a_default(self):
        """A typo in --config must never silently become the default limits."""
        with self.assertRaises(FileNotFoundError):
            settings.load("/no/such/config.json")

    def test_a_broken_file_is_refused_not_replaced_by_the_defaults(self):
        """As the US pack refuses it: the defaults silently standing in for the user's own limits
        would size every trade against rules the user never set."""
        with tempfile.TemporaryDirectory() as tmp, InFolder(tmp) as here:
            (here / "config.json").write_text("{ not json")
            with self.assertRaises(settings.BadConfig):
                settings.load(None, {"a": 1})

    def test_it_never_looks_beside_the_script(self):
        """Installed alone, the folder beside a skill is a folder of OTHER skills."""
        source = (ROOT / "nsetools" / "settings.py").read_text(encoding="utf-8")
        self.assertNotIn("__file__", source)


class TheBudgetAliasActuallyWorks(unittest.TestCase):
    """REGRESSION. A config that set only `capital` was sized against the default 1 lakh.

    The alias checked whether `budget` was missing AFTER the defaults had been merged in, so it
    was never missing. config.example.json sets capital, so this hit everyone using the example.
    """

    def setUp(self):
        self.budget = skill("nse-budget", "size")

    def sized_against(self, written: dict) -> float:
        with tempfile.TemporaryDirectory() as tmp, InFolder(tmp) as here:
            (here / "config.json").write_text(json.dumps(written))
            self.budget.CONFIG_PATH = None
            return self.budget.config()["budget"]

    def test_capital_alone_is_used_as_the_budget(self):
        self.assertEqual(self.sized_against({"capital": 500000}), 500000.0)

    def test_an_explicit_budget_wins_over_capital(self):
        self.assertEqual(self.sized_against({"capital": 500000, "budget": 200000}), 200000)

    def test_neither_means_the_default(self):
        self.assertEqual(self.sized_against({}), self.budget.DEFAULTS["budget"])


@unittest.skipUnless(has("cache/daily-bars.json.gz"), "run capture.py")
class WildersRsiOnRealCloses(unittest.TestCase):
    """Checked against an independent implementation of Wilder's smoothing, on real closes."""

    @staticmethod
    def wilder(closes, n=14):
        """Written separately here, from the definition, not copied from the code under test."""
        if len(closes) < n + 1:
            return None
        changes = [b - a for a, b in zip(closes, closes[1:])]
        gain = sum(max(c, 0) for c in changes[:n]) / n
        loss = sum(max(-c, 0) for c in changes[:n]) / n
        for c in changes[n:]:
            gain = (gain * (n - 1) + max(c, 0)) / n
            loss = (loss * (n - 1) + max(-c, 0)) / n
        return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store, cls.saved = price_cache(Path(cls.tmp.name) / "p.sqlite")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def closes(self, symbol):
        return [b["close"] for b in self.store.series(symbol) if b.get("close")]

    def test_it_matches_the_definition_on_every_real_symbol(self):
        for symbol in self.saved["symbols"]:
            closes = self.closes(symbol)
            self.assertAlmostEqual(setups.rsi(closes), self.wilder(closes), places=9, msg=symbol)

    def test_it_stays_between_nought_and_a_hundred(self):
        for symbol in self.saved["symbols"]:
            value = setups.rsi(self.closes(symbol))
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 100)

    def test_too_few_closes_is_no_rsi_rather_than_a_number(self):
        self.assertIsNone(setups.rsi(self.closes("RELIANCE")[:14]))
        self.assertIsNotNone(setups.rsi(self.closes("RELIANCE")[:15]))

    def test_the_oversold_rules_fire_on_real_history(self):
        fired = {name: sum(1 for s in self.saved["symbols"]
                           for i in range(len(self.store.series(s)))
                           if setups.SETUPS[name](self.store.series(s), i, 20))
                 for name in ("oversold", "oversold-bounce")}
        self.assertGreater(fired["oversold"], 0)
        self.assertGreater(fired["oversold-bounce"], 0)
        self.assertLess(fired["oversold-bounce"], fired["oversold"])

    def test_a_bounce_needs_yesterday_oversold_and_today_up(self):
        for symbol in self.saved["symbols"]:
            series = self.store.series(symbol)
            for i in range(1, len(series)):
                if setups.oversold_bounce(series, i):
                    self.assertLess(setups.rsi(
                        [b["close"] for b in series[:i] if b.get("close")]), 30)
                    self.assertGreater(series[i]["close"], series[i]["previous_close"])


class BarsCloseOnTheIndianGrid(unittest.TestCase):
    """NSE's bars line up with 09:15, not with the hour."""

    def setUp(self):
        self.entry = skill("nse-entry", "entry")

    def test_the_next_close_is_on_the_quarter_past_grid(self):
        for now, due in (("09:10", "09:20"), ("09:15", "09:20"), ("09:17", "09:20"),
                         ("10:12", "10:15"), ("10:15", "10:20"), ("15:27", "15:30")):
            got = self.entry.next_close(clock.now(f"2026-10-22 {now}"), 5)
            self.assertEqual(got.strftime("%H:%M"), due, now)

    def test_one_minute_bars_close_every_minute(self):
        got = self.entry.next_close(clock.now("2026-10-22 10:12:30"), 1)
        self.assertEqual(got.strftime("%H:%M"), "10:13")


if __name__ == "__main__":
    unittest.main(verbosity=2)
