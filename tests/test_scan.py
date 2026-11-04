"""The screener's one job beyond listing stocks: refusing to present a bad rule as a find.

Runs on real market history -- 1,875 daily bars for 25 liquid NSE symbols over 75 trading days --
and on the real intraday session of 2026-10-22 for the opening-range scan.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load import has, price_cache                                  # noqa: E402
from nsetools.store import Prices                                   # noqa: E402

CACHE = "cache/daily-bars.json.gz"


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
    def test_the_rules_are_defined_once_and_neither_skill_keeps_its_own(self):
        """A rule screened on one definition and measured on another is worse than no measurement.

        Both skills take the rules from nsetools.setups. Neither script defines one, and neither
        reaches into the other's folder -- which is what used to happen, and what broke the moment
        either skill was installed alone.
        """
        bt = _load("nse-backtest", "backtest")
        self.assertEqual(sorted(scan.SETUPS), sorted(bt.SETUPS))
        for folder, script in (("nse-scan", "scan.py"), ("nse-backtest", "backtest.py")):
            text = (ROOT / folder / script).read_text(encoding="utf-8")
            for rule in ("def delivery_spike", "def plain_breakout", "def oversold"):
                self.assertNotIn(rule, text, f"{folder} defines its own {rule[4:]}")
            self.assertNotIn('"nse-backtest"', text if folder == "nse-scan" else "")

    def test_the_rsi_setups_the_us_pack_has_are_here(self):
        for name in ("oversold", "oversold-bounce"):
            self.assertIn(name, scan.SETUPS)

    def test_the_turnover_floor_is_shared_too(self):
        bt = _load("nse-backtest", "backtest")
        self.assertEqual(scan.MIN_TURNOVER_LACS, bt.MIN_TURNOVER_LACS)


class FiringOnRealHistory(unittest.TestCase):
    """The screen, run over 1,875 real daily bars for 25 liquid NSE symbols."""

    @classmethod
    def setUpClass(cls):
        if not has(CACHE):
            raise unittest.SkipTest("run capture.py")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store, cls.saved = price_cache(Path(cls.tmp.name) / "p.sqlite")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_every_rule_runs_over_the_real_cache_without_error(self):
        for setup in sorted(scan.SETUPS):
            self.assertIsInstance(scan.firing(self.store, setup, 20), list)

    def test_a_candidate_is_a_real_symbol_with_its_real_numbers(self):
        for setup in sorted(scan.SETUPS):
            for row in scan.firing(self.store, setup, 20):
                self.assertIn(row["symbol"], self.saved["symbols"])
                self.assertGreater(row["close"], 0)
                self.assertGreaterEqual(row["turnover_lacs"], scan.MIN_TURNOVER_LACS)

    def test_no_illiquid_stock_is_ever_a_candidate(self):
        """The floor is shared with the replay, so a screen cannot surface what it cannot measure."""
        for setup in sorted(scan.SETUPS):
            for row in scan.firing(self.store, setup, 20):
                self.assertGreaterEqual(row["turnover_lacs"], scan.MIN_TURNOVER_LACS)

    def test_a_past_day_can_be_scanned(self):
        day = self.store.trading_days()[-5]
        self.assertIsInstance(scan.firing(self.store, "breakout", 20, on=day), list)

    def test_scanning_a_past_day_cannot_see_later_days(self):
        """The screen on an earlier day must not change when later days exist, which they do."""
        day = self.store.trading_days()[-5]
        early = scan.firing(self.store, "breakout", 20, on=day)
        self.assertTrue(all(r["symbol"] in self.saved["symbols"] for r in early))

    def test_a_day_outside_the_cache_is_refused_not_guessed(self):
        with self.assertRaises(SystemExit):
            scan.firing(self.store, "breakout", 20, on="2099-01-01")

    def test_the_real_measured_record_comes_back_with_the_candidates(self):
        """The record is the exchange-measured one stored by a real replay."""
        record = self.store.record_for("delivery-spike")
        self.assertIsNotNone(record)
        self.assertEqual(record["trades"], 57)
        self.assertEqual(record["edge"], 2.67)
        note, trustworthy = scan.record_line(record, "delivery-spike")
        self.assertIn("57", note)
        self.assertFalse(trustworthy)

    def test_a_rule_with_a_negative_measured_edge_is_called_out(self):
        for setup in ("breakout", "accumulation"):
            record = self.store.record_for(setup)
            if record and (record.get("edge") or 0) < 0:
                note, trustworthy = scan.record_line(record, setup)
                self.assertFalse(trustworthy)


class TheIntradayScanOnARealSession(unittest.TestCase):
    """The orb scan is what writes nse/watchlist.json, which nse-entry reads.

    Before it existed, entry.py's watchlist mode read a file nothing ever wrote. Every price here
    is from the real NSE session of 2026-10-22.
    """

    BARS = {s: str(Path(__file__).resolve().parent / "fixtures" / "bars" / f"{s}-5m.json")
            for s in ("SBIN", "ITC", "SUZLON", "RELIANCE", "INFY")}

    @classmethod
    def setUpClass(cls):
        if not all(Path(p).exists() for p in cls.BARS.values()):
            raise unittest.SkipTest("run capture.py")

    def setUp(self):
        from nsetools import bars, clock
        self.bars, self.clock = bars, clock
        self.real = bars.candles
        bars.candles = lambda symbol, *a, **k: self.real(
            symbol, "5m", day=k.get("day"), at=k.get("at"), offline=self.BARS[symbol])

    def tearDown(self):
        self.bars.candles = self.real

    def at(self, hhmm):
        return self.clock.now(f"2026-10-22 {hhmm}")

    def test_the_three_real_breaks_of_that_morning_are_found(self):
        """SBIN at 10:15, ITC at 10:25 and SUZLON at 10:35 really did close above their ranges.

        RELIANCE and INFY did not, all session.
        """
        hits = scan.opening_range_scan(list(self.BARS), self.at("11:00"), min_price=0)
        self.assertEqual({h["symbol"] for h in hits}, {"SBIN", "ITC", "SUZLON"})
        when = {h["symbol"]: h["triggered"] for h in hits}
        self.assertEqual(when["SBIN"], "10:15")
        self.assertEqual(when["ITC"], "10:25")
        self.assertEqual(when["SUZLON"], "10:35")

    def test_the_entry_prices_are_the_real_closes(self):
        hits = {h["symbol"]: h for h in
                scan.opening_range_scan(list(self.BARS), self.at("11:00"), min_price=0)}
        self.assertEqual(hits["SBIN"]["entry"], 961.85)
        self.assertEqual(hits["ITC"]["entry"], 260.85)
        self.assertEqual(hits["SUZLON"]["entry"], 39.18)

    def test_nothing_has_broken_out_before_the_range_has_even_closed(self):
        self.assertEqual(scan.opening_range_scan(list(self.BARS), self.at("09:25"),
                                                 min_price=0), [])

    def test_a_cheap_real_stock_is_filtered_out_by_min_price(self):
        """SUZLON traded at 39, so a 100-rupee floor must drop it and keep SBIN."""
        hits = scan.opening_range_scan(list(self.BARS), self.at("11:00"), min_price=100)
        kept = {h["symbol"] for h in hits}
        self.assertIn("SBIN", kept)
        self.assertNotIn("SUZLON", kept)

    def test_a_volume_floor_drops_the_real_thin_breaks(self):
        """ITC broke on 0.58x and SUZLON on 0.60x of their range volume; SBIN on 1.10x.

        Measured over the range bars whose volume is known. These read 0.87x, 0.90x and 1.66x
        while the 09:15 bar's unreported volume was being counted as zero.
        """
        hits = scan.opening_range_scan(list(self.BARS), self.at("11:00"), min_price=0,
                                       min_volume_x=1.0)
        self.assertEqual({h["symbol"] for h in hits}, {"SBIN"})
        self.assertEqual(scan.opening_range_scan(list(self.BARS), self.at("11:00"),
                                                 min_price=0, min_volume_x=1.5), [])

    def test_hits_are_ordered_by_when_they_broke_earliest_first(self):
        """As the US pack orders them: SBIN 10:15, ITC 10:25, SUZLON 10:35 on 2026-10-22."""
        hits = scan.opening_range_scan(list(self.BARS), self.at("11:00"), min_price=0)
        self.assertEqual([(h["symbol"], h["triggered"]) for h in hits],
                         [("SBIN", "10:15"), ("ITC", "10:25"), ("SUZLON", "10:35")])

    def test_a_hit_carries_its_bar(self):
        sbin = scan.opening_range_scan(["SBIN"], self.at("11:00"), min_price=0)[0]
        self.assertEqual((sbin["bar_start"], sbin["bar_index"], sbin["setups"]),
                         ("10:10", 12, ["orb"]))

    def test_a_chart_with_a_missing_bar_is_not_a_hit(self):
        """A missing bar is nothing traded: a halt or a circuit. The real SBIN session with its
        10:00 bar taken out is refused, though its 10:15 break is untouched."""
        real = self.bars.candles

        def gap(symbol, *a, **k):
            rows = real(symbol, *a, **k)
            return [b for b in rows if b["start"].strftime("%H:%M") != "10:00"]
        self.bars.candles = gap
        try:
            self.assertEqual(scan.opening_range_scan(["SBIN"], self.at("11:00"), min_price=0), [])
        finally:
            self.bars.candles = real

    def test_a_symbol_with_no_bars_is_skipped_not_fatal(self):
        def refuse(*a, **k):
            raise RuntimeError("no bars")
        self.bars.candles = refuse
        self.assertEqual(scan.opening_range_scan(["SBIN"], self.at("11:00"), min_price=0), [])

    def test_the_universe_is_companies_only(self):
        """A fund sits near 100% delivery permanently and would top every screen."""
        self.assertIn("funds", scan.universe.__doc__)


class TwoStagesOverTheWholeMarket(unittest.TestCase):
    """Stage one: closes for twenty symbols a request. Stage two: full bars for the shortlist.

    Run on the real batch responses and the real bars of the same session, 2026-10-22.
    """

    FIX = Path(__file__).resolve().parent / "fixtures"
    NAMES = ["SBIN", "ITC", "SUZLON", "RELIANCE", "INFY"]

    @classmethod
    def setUpClass(cls):
        if not (cls.FIX / "spark" / "batch-a.json").exists():
            raise unittest.SkipTest("run capture.py")
        from nsetools import bars, clock
        cls.bars, cls.clock = bars, clock
        cls.at = clock.now("2026-10-22 11:00")

    def test_twenty_is_the_measured_limit(self):
        """Twenty symbols are accepted and twenty-one are answered 400."""
        self.assertEqual(self.bars.SPARK_MAX, 20)

    def test_the_whole_market_takes_a_hundred_and_eighteen_requests(self):
        self.assertEqual(-(-2359 // self.bars.SPARK_MAX), 118)

    def test_the_batch_returns_closes_and_nothing_else(self):
        """Which is why it can shortlist but not decide: the range high is a high, not a close."""
        got = self.bars.spark(self.NAMES, at=self.at, offline=str(self.FIX / "spark"))
        for rows in got.values():
            for bar in rows:
                self.assertEqual(set(bar), {"start", "end", "close"})

    def test_two_endpoints_agree_on_every_close(self):
        """The batch closes and the full-bar closes are the same numbers, bar for bar."""
        got = self.bars.spark(self.NAMES, at=self.at, offline=str(self.FIX / "spark"))
        self.assertEqual(set(got), set(self.NAMES))
        for symbol, rows in got.items():
            full = self.bars.candles(symbol, "5m", at=self.at,
                                     offline=str(self.FIX / "bars" / f"{symbol}-5m.json"))
            self.assertEqual([b["close"] for b in rows], [b["close"] for b in full], symbol)
            self.assertEqual([b["start"] for b in rows], [b["start"] for b in full], symbol)

    def test_how_the_batches_were_split_does_not_change_the_answer(self):
        """Saved as two requests, three symbols and two. Read together they are one market."""
        one = self.bars.spark(self.NAMES, at=self.at, offline=str(self.FIX / "spark"))
        a = self.bars.spark(self.NAMES, at=self.at,
                            offline=str(self.FIX / "spark" / "batch-a.json"))
        b = self.bars.spark(self.NAMES, at=self.at,
                            offline=str(self.FIX / "spark" / "batch-b.json"))
        self.assertEqual(set(one), set(a) | set(b))
        self.assertFalse(set(a) & set(b))

    def test_the_forming_bar_is_never_in_a_batch(self):
        got = self.bars.spark(["SBIN"], at=self.clock.now("2026-10-22 10:12"),
                              offline=str(self.FIX / "spark"))
        self.assertEqual(got["SBIN"][-1]["end"].strftime("%H:%M"), "10:10")

    def test_stage_one_never_loses_a_real_breakout(self):
        """THE property the design rests on, checked rather than argued.

        A close is never above its own high, so the range's highest close is at or below its true
        high, and anything that closed above the true high must clear the shortlist. Here: every
        symbol stage two really confirms is in stage one's shortlist, at every hour of the session.
        """
        for hhmm in ("09:45", "10:20", "10:40", "11:00", "13:00", "15:00"):
            at = self.clock.now(f"2026-10-22 {hhmm}")
            short = {h["symbol"] for h in scan.shortlist(self.NAMES, at, min_price=0,
                                                        offline=str(self.FIX / "spark"))}
            real = {h["symbol"] for h in scan.opening_range_scan(
                self.NAMES, at, min_price=0, offline=str(self.FIX / "bars"))}
            self.assertTrue(real <= short, f"{hhmm}: lost {real - short}")

    def test_stage_one_does_let_extras_through_which_stage_two_removes(self):
        """It is a shortlist, not a decision. On this session it kept one stock that never broke."""
        short = {h["symbol"] for h in scan.shortlist(self.NAMES, self.at, min_price=0,
                                                    offline=str(self.FIX / "spark"))}
        real = {h["symbol"] for h in scan.opening_range_scan(
            self.NAMES, self.at, min_price=0, offline=str(self.FIX / "bars"))}
        self.assertEqual(real, {"SBIN", "ITC", "SUZLON"})
        self.assertGreater(len(short), len(real))

    def test_no_new_entries_after_the_cutoff(self):
        from datetime import time
        early = scan.opening_range_scan(self.NAMES, self.clock.now("2026-10-22 15:00"),
                                        min_price=0, until=time(10, 20),
                                        offline=str(self.FIX / "bars"))
        self.assertEqual({h["symbol"] for h in early}, {"SBIN"})


class LiquidityRanksStageTwo(unittest.TestCase):
    """Most traded first, from the PREVIOUS day, so the slots go to stocks that can be traded."""

    def test_offline_liquidity_comes_from_the_exchanges_own_file(self):
        fix = Path(__file__).resolve().parent / "fixtures"
        if not (fix / "exchange" / "bhavcopy.json.gz").exists():
            self.skipTest("run capture.py")
        from nsetools import clock
        # The saved file is 2026-10-23's, so the first session it can rank is the next one.
        traded, day = scan.liquidity(clock.now("2026-10-26 11:00"), str(fix))
        self.assertGreater(len(traded), 2000)
        self.assertEqual(day, "2026-10-23")
        # The three "breakouts" the live scan wrongly surfaced first were among the least traded
        # stocks on the exchange. Ranked by turnover they come nowhere near the top.
        rank = sorted(traded, key=lambda s: -traded[s])
        self.assertLess(rank.index("RELIANCE"), 20)
        for thin in ("SHRADHA", "3PLAND", "MOTOGENFIN"):
            self.assertGreater(rank.index(thin), 2000, thin)
            self.assertLess(traded[thin], scan.MIN_TURNOVER_LACS, thin)

    def test_it_asks_for_the_day_before_never_the_same_day(self):
        """Today's closing turnover is not known at 11:00, so ranking on it would see the future."""
        import inspect
        source = inspect.getsource(scan.liquidity)
        self.assertIn("previous_trading_day(at.date())", source)

    def test_no_liquidity_means_no_ranking_said_out_loud(self):
        from nsetools import clock
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(scan.liquidity(clock.now("2026-10-23 11:00"), tmp), ({}, ""))

    def test_a_saved_file_from_the_replayed_day_or_later_is_never_used(self):
        """Replaying 2026-10-22 on 2026-10-23's turnover ranked a morning on figures nobody had."""
        fix = Path(__file__).resolve().parent / "fixtures"
        if not (fix / "exchange" / "bhavcopy.json.gz").exists():
            self.skipTest("run capture.py")
        from nsetools import clock
        for when in ("2026-10-22 11:00", "2026-10-23 11:00"):
            self.assertEqual(scan.liquidity(clock.now(when), str(fix)), ({}, ""), when)


FIX = Path(__file__).resolve().parent / "fixtures"
NAMES = "SBIN,ITC,SUZLON,RELIANCE,INFY"


def run_main(argv):
    """scan.main with its printed output captured."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = scan.main(argv)
    return code, buf.getvalue()


def fixture_copy(tmp: Path, drop_bars=(), spark_edit=None) -> Path:
    """A copy of the real saved responses with one fact changed."""
    import json
    import shutil
    home = tmp / "fx"
    shutil.copytree(FIX / "spark", home / "spark")
    shutil.copytree(FIX / "bars", home / "bars")
    for symbol in drop_bars:
        (home / "bars" / f"{symbol}-5m.json").unlink()
    if spark_edit:
        for path in (home / "spark").glob("*.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            if spark_edit(doc):
                path.write_text(json.dumps(doc), encoding="utf-8")
    return home


def drop_spark_close(symbol: str, day: str, hhmm: str):
    """An editor that removes one real close from the saved batch answer."""
    from datetime import datetime
    from nsetools.clock import IST

    def edit(doc):
        body = doc.get("data", doc) if "_source" in doc else doc
        changed = False
        for result in body["spark"]["result"]:
            if result["symbol"] != f"{symbol}.NS":
                continue
            series = result["response"][0]
            closes = series["indicators"]["quote"][0]["close"]
            for k, stamp in enumerate(series["timestamp"]):
                at = datetime.fromtimestamp(stamp, IST)
                if at.date().isoformat() == day and at.strftime("%H:%M") == hhmm:
                    closes[k] = None
                    changed = True
        return changed
    return edit


@unittest.skipUnless((FIX / "spark" / "batch-a.json").exists(), "run capture.py")
class NothingIsDroppedQuietly(unittest.TestCase):
    """The audit's gaps, each on the real session of 2026-10-22 with one fact changed."""

    def test_a_scan_with_no_hits_still_writes_the_watchlist(self):
        """Writing only on a hit left the last run's hits for nse-entry to re-check."""
        import json
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "watchlist.json"
            run_main(["orb", "--symbols", "SBIN,ITC", "--offline", str(FIX),
                      "--at", "2026-10-22 11:00", "--out", str(out)])
            self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))["hits"]), 2)
            code, _ = run_main(["orb", "--symbols", "SBIN,ITC", "--offline", str(FIX),
                                "--at", "2026-10-23 09:35", "--out", str(out)])
            saved = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(code, 1)
            self.assertEqual(saved["hits"], [])
            self.assertEqual(saved["as_of"], "2026-10-23T09:35:00+05:30")

    def test_the_watchlist_carries_the_reference_fields(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "w.json"
            run_main(["orb", "--symbols", NAMES, "--offline", str(FIX), "--at",
                      "2026-10-22 11:00", "--out", str(out), "--or-minutes", "15"])
            saved = json.loads(out.read_text(encoding="utf-8"))
        for key in ("created_by", "mode", "or_minutes", "interval", "scanned", "failed",
                    "unconfirmed", "filters", "note", "hits"):
            self.assertIn(key, saved, key)
        self.assertEqual((saved["mode"], saved["or_minutes"], saved["interval"]), ("orb", 15, "5m"))
        self.assertEqual((saved["scanned"], saved["failed"], saved["confirmed"]), (5, 0, 4))

    def test_created_at_is_the_clock_not_the_cutoff(self):
        """The US pack stamps the file with its clock (--now, or the real time) and the cutoff
        goes in as_of: a 11:00 cutoff run at 12:00 was created at 12:00, not 11:00."""
        import json
        from datetime import datetime, timedelta, timezone
        code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(FIX), "--at", "11:00",
                               "--now", "2026-10-22 12:00", "--no-write", "--json"])
        doc = json.loads(said)
        self.assertEqual((doc["created_at"], doc["as_of"]),
                         ("2026-10-22T06:30:00+00:00", "2026-10-22T11:00:00+05:30"))
        code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(FIX), "--at",
                               "2026-10-22 11:00", "--no-write", "--json"])
        stamped = datetime.fromisoformat(json.loads(said)["created_at"])
        self.assertLess(abs(datetime.now(timezone.utc) - stamped), timedelta(minutes=5))

    def test_json_prints_the_whole_document_with_its_counts(self):
        import json
        code, said = run_main(["orb", "--symbols", NAMES, "--offline", str(FIX), "--at",
                               "2026-10-22 11:00", "--no-write", "--json"])
        doc = json.loads(said)
        self.assertEqual(code, 0)
        self.assertEqual(doc["mode"], "orb")
        self.assertEqual(doc["counts"]["hits"], 3)
        self.assertEqual(doc["counts"]["confirmed"], 4)

    def test_a_chart_that_cannot_be_read_is_counted_and_named(self):
        """ITC's real bars taken away: it is neither a hit nor silently gone."""
        import json
        with tempfile.TemporaryDirectory() as tmp:
            home = fixture_copy(Path(tmp), drop_bars=["ITC"])
            out = Path(tmp) / "w.json"
            code, said = run_main(["orb", "--symbols", NAMES, "--offline", str(home), "--at",
                                   "2026-10-22 11:00", "--out", str(out)])
            saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual({h["symbol"] for h in saved["hits"]}, {"SBIN", "SUZLON"})
        self.assertIn("ITC", saved["failed_symbols"])
        self.assertEqual((saved["failed"], saved["confirmed"]), (1, 3))
        self.assertIn("1 could not be read", said)
        self.assertIn("COULD NOT READ: ITC", said)

    def test_a_failed_scan_never_reads_as_nothing_broke(self):
        """A session the files do not hold: every chart failed, and it says so."""
        code, said = run_main(["orb", "--symbols", "SBIN,ITC", "--offline", str(FIX), "--at",
                               "2026-10-12 11:00", "--no-write"])
        self.assertEqual(code, 1)
        self.assertNotIn("Nothing has closed", said)
        self.assertIn("failed scan, not an empty one", said)
        self.assertIn("Fetched 0 of 2 charts (2 failed or had no data)", said)

    def test_a_missing_range_bar_in_the_batch_feed_loses_nothing(self):
        """INFY broke at 09:35 on 2026-10-16. With its 09:15 batch close removed, the range used
        to start at 09:20, the break fell inside it, and stage one lost it."""
        from nsetools import clock
        with tempfile.TemporaryDirectory() as tmp:
            home = fixture_copy(Path(tmp), spark_edit=drop_spark_close("INFY", "2026-10-16",
                                                                       "09:15"))
            at = clock.now("2026-10-16 09:35")
            short = scan.shortlist(["INFY"], at, min_price=0, offline=str(home / "spark"))
            self.assertEqual([h["symbol"] for h in short], ["INFY"])
            self.assertEqual(short[0]["range_partial"], ["09:15"])
            import json
            out = Path(tmp) / "w.json"
            run_main(["orb", "--symbols", "INFY", "--offline", str(home), "--at",
                      "2026-10-16 09:35", "--out", str(out)])
            hit = json.loads(out.read_text(encoding="utf-8"))["hits"][0]
        self.assertEqual((hit["bar_start"], hit["stage1_range_partial"]), ("09:30", ["09:15"]))
        self.assertEqual(hit["bar_index"], 4)                     # counted from 1: 09:30 is bar 4

    def test_the_last_range_bar_missing_does_not_drop_the_symbol(self):
        from nsetools import clock
        with tempfile.TemporaryDirectory() as tmp:
            home = fixture_copy(Path(tmp), spark_edit=drop_spark_close("SBIN", "2026-10-22",
                                                                       "09:25"))
            short = scan.shortlist(["SBIN"], clock.now("2026-10-22 11:00"), min_price=0,
                                   offline=str(home / "spark"))
        self.assertEqual([h["symbol"] for h in short], ["SBIN"])
        self.assertEqual(short[0]["range_partial"], ["09:25"])

    def test_stage_one_reports_what_came_back(self):
        from nsetools import clock
        report = {}
        scan.shortlist(["SBIN", "NOSUCH"], clock.now("2026-10-22 11:00"), min_price=0,
                       offline=str(FIX / "spark"), report=report)
        self.assertEqual((report["asked"], report["fetched"]), (2, 1))
        self.assertIn("NOSUCH", report["failed"])


class TheClockRefuses(unittest.TestCase):
    def test_a_live_scan_in_the_future_is_refused(self):
        """The bar still forming would be read as closed."""
        from datetime import timedelta
        from nsetools import clock
        later = clock.now() + timedelta(days=2)
        code, said = run_main(["orb", "--symbols", "SBIN", "--at", f"{later:%Y-%m-%d %H:%M}",
                               "--no-write"])
        self.assertEqual(code, 2)
        self.assertIn("later than now", said)

    def test_a_live_scan_more_than_six_days_back_is_refused(self):
        from datetime import timedelta
        from nsetools import clock
        old = clock.now() - timedelta(days=10)
        while not clock.is_trading_day(old.date(), offline=True):    # a weekend would refuse first
            old -= timedelta(days=1)
        code, said = run_main(["orb", "--symbols", "SBIN", "--at", f"{old:%Y-%m-%d} 11:00",
                               "--no-write"])
        self.assertEqual(code, 2)
        self.assertIn("--offline", said)

    @unittest.skipUnless((FIX / "spark" / "batch-a.json").exists(), "run capture.py")
    def test_a_bare_time_cuts_the_clock_as_the_us_pack_does(self):
        """`orb --at 10:15` is the reference's own command. Offline with no --now it replays the
        last session in the saved files (2026-10-23 here); with --now, that day."""
        import json
        code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(FIX), "--at", "11:00",
                               "--no-write", "--json"])
        self.assertEqual(json.loads(said)["as_of"], "2026-10-23T11:00:00+05:30")
        code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(FIX), "--at", "11:00",
                               "--now", "2026-10-22 12:00", "--no-write", "--json"])
        doc = json.loads(said)
        self.assertEqual((code, doc["as_of"], [h["triggered"] for h in doc["hits"]]),
                         (0, "2026-10-22T11:00:00+05:30", ["10:15"]))
        code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(FIX), "--at", "13:00",
                               "--now", "2026-10-22 12:00", "--no-write"])
        self.assertEqual(code, 2)
        self.assertIn("later than --now", said)
        for bad in ("25:00", "10:75"):
            code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(FIX), "--at", bad,
                                   "--no-write"])
            self.assertEqual(code, 2, bad)
            self.assertNotIn("Traceback", said)

    def test_a_bare_time_for_daily_is_refused_plainly(self):
        code, said = run_main(["daily", "--live", "--symbols", "SBIN", "--at", "10:15",
                               "--no-write"])
        self.assertEqual(code, 2)
        self.assertIn("--at wants a date and a time", said)


class ReadingSymbolLists(unittest.TestCase):
    def test_text_json_and_watchlist_files_all_read(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "a.txt").write_text("# mine\nSBIN|State Bank of India\n\nitc\n",
                                       encoding="utf-8")
            (tmp / "b.json").write_text(json.dumps(["SBIN", "ITC"]), encoding="utf-8")
            (tmp / "c.json").write_text(json.dumps({"hits": [{"symbol": "SBIN"}, {"x": 1}]}),
                                        encoding="utf-8")
            self.assertEqual(scan.symbols_from_file(tmp / "a.txt"), ["SBIN", "ITC"])
            self.assertEqual(scan.symbols_from_file(tmp / "b.json"), ["SBIN", "ITC"])
            self.assertEqual(scan.symbols_from_file(tmp / "c.json"), ["SBIN"])

    def test_a_missing_or_broken_file_is_one_plain_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            for path in (Path(tmp) / "nope.json", bad):
                code, said = run_main(["orb", "--watchlist", str(path), "--offline", str(FIX),
                                       "--at", "2026-10-22 11:00", "--no-write"])
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", said)
                self.assertIn("scan.py:", said)

    def test_a_universe_that_cannot_be_fetched_is_said(self):
        real = scan.universe

        real = scan.universe_rows

        def down(limit=None, offline=None):
            raise OSError("network unreachable")
        scan.universe_rows = down
        try:
            code, said = run_main(["universe", "--no-write"])
        finally:
            scan.universe_rows = real
        self.assertEqual(code, 1)
        self.assertIn("Could not fetch the NSE equity list", said)


@unittest.skipUnless((FIX / "spark-daily" / "batch-a.json").exists(), "run capture.py")
class TheDailyScanOffline(unittest.TestCase):
    def test_it_scans_the_charts_in_the_files_and_dates_itself_by_the_clock(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "d.json"
            code, said = run_main(["daily", "--live", "--offline", str(FIX), "--at",
                                   "2026-10-24 08:00", "--out", str(out), "--min-volume", "0"])
            saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual((saved["universe"], saved["failed"]), (9, 0))
        self.assertEqual(saved["created_at"], "2026-10-24T02:30:00+00:00")
        self.assertIn("Fetched 9 of 9 charts.", said)


@unittest.skipUnless(has(CACHE), "run capture.py")
class TheCachedDailyScanHandsOn(unittest.TestCase):
    def test_it_writes_the_watchlist_and_exits_by_whether_anything_fired(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = price_cache(Path(tmp) / "p.sqlite")
            day = store.trading_days()[-1]
            store.close()
            for setup in sorted(scan.SETUPS):
                out = Path(tmp) / f"{setup}.json"
                code, _ = run_main(["--setup", setup, "--db", str(Path(tmp) / "p.sqlite"),
                                    "--out", str(out)])
                saved = json.loads(out.read_text(encoding="utf-8"))
                self.assertEqual(saved["mode"], "daily")
                self.assertEqual(saved["as_of"], f"{day} close")
                self.assertEqual(code, 0 if saved["hits"] else 1, setup)
                for hit in saved["hits"]:
                    self.assertEqual(hit["setups"], [setup])


def edit_series(folder: Path, symbol: str, change) -> None:
    """Apply `change(series)` to one symbol's saved batch answer in a copied folder."""
    import json
    for path in folder.glob("*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        body = doc.get("data", doc) if "_source" in doc else doc
        hit = False
        for result in body["spark"]["result"]:
            if result["symbol"] == f"{symbol}.NS":
                change(result["response"][0])
                hit = True
        if hit:
            path.write_text(json.dumps(doc), encoding="utf-8")


@unittest.skipUnless((FIX / "spark" / "batch-a.json").exists()
                     and (FIX / "spark-daily" / "batch-a.json").exists(), "run capture.py")
class TheThirdSideBySidePass(unittest.TestCase):
    """Each gap the side-by-side check against the US pack found, on real files with one fact
    changed."""

    def setUp(self):
        import shutil
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "fx"
        for part in ("spark", "spark-daily", "bars"):
            shutil.copytree(FIX / part, self.home / part)

    def tearDown(self):
        self.tmp.cleanup()

    def orb(self, *extra, at="2026-10-22 11:00"):
        return run_main(["orb", "--offline", str(self.home), "--at", at, "--no-write", *extra])

    # ---- the daily closes ----------------------------------------------------------------------

    def test_a_second_close_for_the_same_day_does_not_change_the_scan(self):
        """The feed can send the day's bar and then a live tick dated the same day. The US pack
        keeps one close a day; two read as a flat day and TCS's real breakout was lost."""
        from nsetools import bars, clock
        at = clock.now("2026-10-24 08:00")
        before = bars.spark_daily(["TCS"], at=at, offline=str(self.home / "spark-daily"))["TCS"]

        def tick(series):
            series["timestamp"].append(series["timestamp"][-1] + 3600)
            series["indicators"]["quote"][0]["close"].append(
                series["indicators"]["quote"][0]["close"][-1])
        edit_series(self.home / "spark-daily", "TCS", tick)
        after = bars.spark_daily(["TCS"], at=at, offline=str(self.home / "spark-daily"))["TCS"]
        self.assertEqual(after["closes"], before["closes"])
        hits, _, _ = scan.daily_live(["TCS"], at, 0, 0, str(self.home / "spark-daily"), 0)
        self.assertIn("breakout", hits[0]["setups"])

    def test_a_daily_scan_that_read_nothing_is_a_failed_scan_not_no_setups(self):
        code, said = run_main(["daily", "--live", "--symbols", "NOSUCH,ZZZZ", "--offline",
                               str(self.home), "--at", "2026-10-24 08:00", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("failed scan, not an empty one", said)
        self.assertNotIn("No setups.", said)
        self.assertIn("COULD NOT READ: NOSUCH, ZZZZ", said)

    def test_the_daily_scan_says_each_rule_in_words_as_the_us_pack_does(self):
        code, said = run_main(["daily", "--live", "--offline", str(self.home), "--at",
                               "2026-10-24 08:00", "--no-write"])
        self.assertEqual(code, 0)
        self.assertIn("4 charts matched a setup: 2 20-day closing-high breakout, 2 bull flag, "
                      "1 oversold (RSI under 30).", said)

    # ---- the five-minute closes ----------------------------------------------------------------

    def test_an_off_grid_tick_and_a_repeated_bar_are_not_bars(self):
        from nsetools import bars, clock
        at = clock.now("2026-10-22 15:00")
        before = bars.spark(["SBIN"], at=at, offline=str(self.home / "spark"))["SBIN"]

        def tick(series):
            stamps, closes = series["timestamp"], series["indicators"]["quote"][0]["close"]
            k = len(stamps) // 2
            stamps.insert(k + 1, stamps[k] + 127)                # 2 min 7 s into a bar
            closes.insert(k + 1, closes[k])
            stamps.append(stamps[-1])                            # the last bar sent twice
            closes.append(closes[-1])
        edit_series(self.home / "spark", "SBIN", tick)
        after = bars.spark(["SBIN"], at=at, offline=str(self.home / "spark"))["SBIN"]
        self.assertEqual([b["start"] for b in after], [b["start"] for b in before])

    def test_a_batch_answer_with_nothing_after_the_range_goes_to_stage_two(self):
        """SBIN's batch closes after 09:30 removed: stage one cannot rule it out, so the full
        chart decides, and its real 10:15 break is still found."""
        from datetime import datetime
        from nsetools.clock import IST

        def cut(series):
            closes = series["indicators"]["quote"][0]["close"]
            for k, stamp in enumerate(series["timestamp"]):
                when = datetime.fromtimestamp(stamp, IST)
                if when.date().isoformat() == "2026-10-22" and when.strftime("%H:%M") >= "09:30":
                    closes[k] = None
        edit_series(self.home / "spark", "SBIN", cut)
        code, said = self.orb("--symbols", "SBIN", "--json")
        import json
        doc = json.loads(said)
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["SBIN"])

    # ---- what the files say --------------------------------------------------------------------

    def test_a_hit_and_the_counts_carry_the_us_packs_names_too(self):
        import json
        code, said = self.orb("--symbols", NAMES, "--json")
        doc = json.loads(said)
        sbin = next(h for h in doc["hits"] if h["symbol"] == "SBIN")
        self.assertEqual((sbin["price"], sbin["at"], sbin["or_high"], sbin["or_low"],
                          sbin["pct_over"]), (961.85, "10:15", 959.3, 950.05, 0.27))
        for key in ("requested", "scanned", "candidates", "confirmed", "unconfirmed", "illiquid",
                    "hits"):
            self.assertIn(key, doc["counts"], key)
        self.assertEqual((doc["counts"]["candidates"], doc["counts"]["unconfirmed"]), (4, 0))

    def test_a_price_floor_is_counted_not_silent(self):
        """SUZLON at 39 under a 100-rupee floor: scanned is the charts above the floor."""
        import json
        code, said = self.orb("--symbols", NAMES, "--json", "--min-price", "100")
        doc = json.loads(said)
        self.assertEqual((doc["scanned"], doc["counts"]["illiquid"]), (4, 1))

    def test_a_scan_with_candidates_left_unchecked_never_says_nothing_broke(self):
        """RELIANCE and INFY with room for one check: the one checked did not break, and the
        other was never looked at. That is not "no chart closed above its range"."""
        code, said = self.orb("--symbols", NAMES, "--max-confirm", "1", at="2026-10-22 09:50")
        self.assertEqual(code, 1)
        self.assertIn("PARTIAL: 2 more candidates were not checked", said)
        self.assertNotIn("No chart closed above", said)
        self.assertIn("2 were not checked, so this is not the whole list", said)

    def test_offline_with_no_list_scans_every_chart_in_the_files(self):
        import json
        code, said = self.orb("--json")
        self.assertEqual(json.loads(said)["universe"], 5)

    def test_no_list_and_no_universe_file_is_refused_not_some_other_list(self):
        """The reference refuses; this used to read nse/scan.json, which SKILL.md says nobody
        reads."""
        import os
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as empty:
            os.chdir(empty)
            try:
                code, said = run_main(["orb", "--at", "2026-10-22 11:00", "--no-write",
                                       "--offline", str(self.home), "--universe"])
                code2, said2 = run_main(["daily", "--live", "--universe", "nope.txt",
                                         "--offline", str(self.home), "--at",
                                         "2026-10-24 08:00", "--no-write"])
            finally:
                os.chdir(here)
        self.assertEqual((code, code2), (2, 2))
        self.assertIn("run `scan.py universe` first", said)
        self.assertIn("no nope.txt yet", said2)

    def test_symbols_split_on_spaces_and_an_empty_list_is_refused(self):
        import json
        code, said = self.orb("--symbols", "SBIN ITC", "--json")
        self.assertEqual(json.loads(said)["universe"], 2)
        code, said = self.orb("--symbols", " , ")
        self.assertEqual(code, 2)
        self.assertIn("no symbols to scan", said)

    def test_symbols_and_a_watchlist_together_are_refused(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            scan.main(["orb", "--symbols", "SBIN", "--watchlist", "w.json"])
        self.assertEqual(stop.exception.code, 2)

    def test_a_negative_count_is_refused(self):
        import contextlib
        import io
        for flag in ("--limit", "--max-confirm", "--show", "--pause"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                scan.main(["orb", flag, "-1"])

    def test_the_us_packs_defaults(self):
        self.assertEqual((scan.MAX_CONFIRM, scan.PAUSE, scan.MIN_PRICE), (200, 0.3, 20.0))

    # ---- hostile input -------------------------------------------------------------------------

    def test_a_missing_or_damaged_offline_folder_is_one_plain_line(self):
        (self.home / "spark" / "batch-a.json").write_text("{not json", encoding="utf-8")
        for folder, words in ((self.home, "not a readable saved answer"),
                              (self.home / "nope", "no offline folder")):
            code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(folder), "--at",
                                   "2026-10-22 11:00", "--no-write"])
            self.assertEqual(code, 2, folder)
            self.assertIn(words, said)
            self.assertNotIn("Traceback", said)

    def test_a_config_that_does_not_exist_is_one_plain_line(self):
        code, said = self.orb("--symbols", "SBIN", "--config", str(self.home / "none.json"))
        self.assertEqual(code, 2)
        self.assertIn("no config at", said)

    def test_a_damaged_turnover_file_means_no_ranking_said(self):
        from nsetools import clock
        (self.home / "exchange").mkdir()
        (self.home / "exchange" / "bhavcopy.json.gz").write_bytes(b"not gzip")
        self.assertEqual(scan.liquidity(clock.now("2026-10-26 11:00"), str(self.home)), ({}, ""))

    def test_stage_two_pauses_between_live_requests(self):
        from nsetools import bars, clock
        real_candles, real_sleep, slept = bars.candles, scan.time.sleep, []
        bars.candles = lambda symbol, *a, **k: real_candles(
            symbol, "5m", at=k.get("at"), offline=str(self.home / "bars" / f"{symbol}-5m.json"))
        scan.time.sleep = slept.append
        try:
            scan.opening_range_scan(["SBIN", "ITC", "INFY"], clock.now("2026-10-22 11:00"),
                                    min_price=0, pause=0.3)
        finally:
            bars.candles, scan.time.sleep = real_candles, real_sleep
        self.assertEqual(slept, [0.3, 0.3])


def run_main_err(argv):
    """scan.main with stdout and stderr captured apart."""
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless((FIX / "spark" / "batch-a.json").exists()
                     and (FIX / "spark-daily" / "batch-a.json").exists(), "run capture.py")
class TheFourthSideBySidePass(unittest.TestCase):
    """Each gap the fourth side-by-side check found, on real files with one fact changed."""

    def setUp(self):
        import shutil
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "fx"
        for part in ("spark", "spark-daily", "bars"):
            shutil.copytree(FIX / part, self.home / part)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_close_level_with_the_range_is_a_candidate_as_the_us_pack_counts_it(self):
        """ITC on 2026-10-21, 30-minute range: a later close of 264.40 equals the range's highest
        close exactly. The reference counts it a candidate (>=), stage two finds no break."""
        from nsetools import clock
        at = clock.now("2026-10-21 10:35")
        short = scan.shortlist(["ITC"], at, 30, min_price=0, offline=str(FIX / "spark"))
        self.assertEqual([(h["symbol"], h["lead"]) for h in short], [("ITC", 0.0)])
        code, said = run_main(["orb", "--symbols", "ITC", "--offline", str(FIX), "--at",
                               "2026-10-21 10:35", "--or-minutes", "30", "--min-price", "0",
                               "--no-write", "--json"])
        import json
        doc = json.loads(said)
        self.assertEqual((code, doc["counts"]["candidates"], doc["counts"]["confirmed"],
                          doc["hits"]), (1, 1, 1, []))

    def test_the_cutoff_is_on_the_minute(self):
        """--now 15:00:40 with no_entries_after 15:00 is a 15:00 scan, as the US pack cuts it,
        not a refusal; and as_of carries no seconds."""
        import json
        code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(self.home), "--now",
                               "2026-10-22 15:00:40", "--until", "15:00", "--no-write", "--json"])
        doc = json.loads(said)
        self.assertEqual((code, doc["as_of"]), (0, "2026-10-22T15:00:00+05:30"))

    def test_scanned_and_under_the_floor_add_up_to_what_came_back(self):
        """A volume floor stage two applies: the charts under it are not also in `scanned`."""
        import json
        code, said = run_main(["orb", "--symbols", NAMES, "--offline", str(self.home), "--at",
                               "2026-10-22 11:00", "--min-volume", "20000000", "--no-write",
                               "--json"])
        c = json.loads(said)["counts"]
        self.assertGreater(c["illiquid"], 0)
        self.assertEqual(c["scanned"] + c["illiquid"], c["fetched"])
        code, said = run_main(["orb", "--symbols", NAMES, "--offline", str(self.home), "--at",
                               "2026-10-22 11:00", "--min-volume", "20000000", "--no-write"])
        self.assertIn(f"; {c['scanned']} passed the price / volume floor (Rs 20, 20,000,000 "
                      f"shares) ({c['illiquid']} under it)", said)

    def test_a_chart_with_a_missing_bar_is_counted_unusable(self):
        import json
        path = self.home / "bars" / "SBIN-5m.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        series = doc["chart"]["result"][0]
        from datetime import datetime
        from nsetools.clock import IST
        for k, stamp in enumerate(series["timestamp"]):
            when = datetime.fromtimestamp(stamp, IST)
            if when.strftime("%Y-%m-%d %H:%M") == "2026-10-22 10:00":
                series["indicators"]["quote"][0]["close"][k] = None
        path.write_text(json.dumps(doc), encoding="utf-8")
        code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(self.home), "--at",
                               "2026-10-22 11:00", "--no-write", "--json"])
        doc = json.loads(said)
        self.assertEqual((doc["counts"]["unusable"], doc["hits"]), (1, []))
        self.assertIn("missing bar", doc["failed_symbols"]["SBIN"])

    def test_ctrl_c_is_one_line_not_a_traceback(self):
        real = scan.orb

        def stop(*_a, **_k):
            raise KeyboardInterrupt
        scan.orb = stop
        try:
            code, out, err = run_main_err(["orb", "--symbols", "SBIN", "--offline", str(FIX),
                                           "--at", "2026-10-22 11:00", "--no-write"])
        finally:
            scan.orb = real
        self.assertEqual((code, err.strip()), (1, "scan.py: stopped"))

    def test_a_floor_is_printed_as_the_us_pack_prints_it(self):
        for given, shown in (("20.5", "Rs 20.50+"), ("1000000", "Rs 1000000+")):
            code, said = run_main(["daily", "--live", "--offline", str(self.home), "--at",
                                   "2026-10-24 08:00", "--min-price", given, "--no-write"])
            self.assertIn(shown, said, given)

    def test_a_symbol_named_with_no_data_is_a_failure_not_an_illiquid_chart(self):
        """TCS's real daily answer with its data emptied: the US pack counts it failed."""
        for path in (self.home / "spark-daily").glob("*.json"):
            import json
            doc = json.loads(path.read_text(encoding="utf-8"))
            for result in doc["spark"]["result"]:
                if result["symbol"] == "TCS.NS":
                    result["response"] = []
            path.write_text(json.dumps(doc), encoding="utf-8")
        from nsetools import clock
        hits, counts, _ = scan.daily_live(["TCS", "SBIN"], clock.now("2026-10-24 08:00"), 0, 0,
                                          str(self.home / "spark-daily"), 0)
        self.assertEqual((counts["fetched"], counts["illiquid"]), (1, 0))
        self.assertIn("TCS", counts["failed_symbols"])

    def test_a_saved_file_that_is_not_a_batch_answer_is_refused_plainly(self):
        import json
        import shutil
        shutil.copy(FIX / "bars" / "SBIN-5m.json", self.home / "spark" / "chart.json")
        (self.home / "spark-daily" / "list.json").write_text(json.dumps(["SBIN"]),
                                                             encoding="utf-8")
        for argv in (["orb", "--symbols", "SBIN", "--at", "2026-10-22 11:00"],
                     ["daily", "--live", "--symbols", "SBIN", "--at", "2026-10-24 08:00"]):
            code, said = run_main(argv + ["--offline", str(self.home), "--no-write"])
            self.assertEqual(code, 2, argv)
            self.assertIn("is not a saved batch answer", said)
            self.assertNotIn("Traceback", said)

    def test_a_nan_close_is_a_gap_not_a_close_that_hides_a_breakout(self):
        """SBIN's real 09:15 batch close written as NaN. max() over a NaN range ruled the real
        10:15 break out in stage one; read as a gap, the symbol is kept."""
        from datetime import datetime
        from nsetools import clock
        from nsetools.clock import IST

        def poison(series):
            closes = series["indicators"]["quote"][0]["close"]
            for k, stamp in enumerate(series["timestamp"]):
                if datetime.fromtimestamp(stamp, IST).strftime("%Y-%m-%d %H:%M") == \
                        "2026-10-22 09:15":
                    closes[k] = float("nan")
        edit_series(self.home / "spark", "SBIN", poison)
        short = scan.shortlist(["SBIN"], clock.now("2026-10-22 11:00"), min_price=0,
                               offline=str(self.home / "spark"))
        self.assertEqual([(h["symbol"], h.get("range_partial")) for h in short],
                         [("SBIN", ["09:15"])])

    def test_a_live_fetch_counts_a_refused_batch_and_reports_progress(self):
        """The network simulated in-process with the real saved answer: one batch answered, one
        refused. The refused symbols are failures, named, and the progress count is told."""
        from nsetools import bars, clock
        saved = (FIX / "spark" / "batch-a.json").read_bytes()
        calls, told = [], []

        def fake(url, timeout):
            calls.append(url)
            if len(calls) == 2:
                raise bars.NoBars("the feed answered HTTP 500")
            return saved
        real = bars._get
        bars._get = fake
        try:
            failed = {}
            names = ["SBIN"] + [f"X{k:02d}" for k in range(25)]
            got = bars.spark(names, at=clock.now("2026-10-22 11:00"), pause=0, failed=failed,
                             progress=lambda done, total: told.append((done, total)))
        finally:
            bars._get = real
        self.assertEqual(len(calls), 2)
        self.assertIn("SBIN", got)
        self.assertEqual(told, [(1, 2), (2, 2)])
        self.assertTrue(all("HTTP 500" in failed[s] for s in names[20:]))

    def test_an_offline_replay_prints_no_fetching_line(self):
        code, out, err = run_main_err(["daily", "--live", "--offline", str(self.home), "--at",
                                       "2026-10-24 08:00", "--no-write"])
        self.assertNotIn("Fetching", err)

    def test_a_symbol_with_no_turnover_figure_is_not_called_too_thin(self):
        """The real 2026-10-23 bhavcopy with ITC's row taken out, ranking the 2026-10-22 session
        in-process: ITC has no figure, so it is kept and ranked last, and its real break found."""
        import gzip
        import json
        body = json.loads(gzip.decompress((FIX / "exchange" / "bhavcopy.json.gz").read_bytes()))
        rows = body.get("data", body)["rows"]
        traded = {r["symbol"]: r["turnover"] for r in rows if r["symbol"] != "ITC"}
        real = scan.liquidity
        scan.liquidity = lambda at, offline=None: (traded, "2026-10-23")
        try:
            code, said = run_main(["orb", "--symbols", NAMES, "--offline", str(self.home),
                                   "--at", "2026-10-22 11:00", "--no-write", "--json"])
        finally:
            scan.liquidity = real
        doc = json.loads(said)
        self.assertEqual((doc["too_thin"], doc["counts"]["no_turnover_figure"]), (0, 1))
        self.assertIn("ITC", {h["symbol"] for h in doc["hits"]})

    def test_a_candidate_dropped_as_too_thin_is_never_read_as_nothing_broke(self):
        """The real 2026-10-23 turnovers with RELIANCE's cut to 50 lakh, ranking the real 09:50
        scan of 2026-10-22 in-process: RELIANCE was dropped, not checked, so the scan must not
        say no chart closed above its range."""
        import gzip
        import json
        body = json.loads(gzip.decompress((FIX / "exchange" / "bhavcopy.json.gz").read_bytes()))
        traded = {r["symbol"]: r["turnover"] for r in body.get("data", body)["rows"]}
        traded["RELIANCE"] = 50.0
        real = scan.liquidity
        scan.liquidity = lambda at, offline=None: (traded, "2026-10-23")
        try:
            code, said = run_main(["orb", "--symbols", NAMES, "--offline", str(self.home),
                                   "--at", "2026-10-22 09:50", "--no-write"])
        finally:
            scan.liquidity = real
        self.assertEqual(code, 1)
        self.assertNotIn("No chart closed above", said)
        self.assertIn("1 were dropped as too thinly traded, so this is not the whole list", said)

    def test_a_weekend_says_what_to_do_instead(self):
        code, said = run_main(["orb", "--symbols", "SBIN", "--offline", str(self.home), "--at",
                               "2026-10-24 11:00", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("NSE CLOSED on Sat 2026-10-24", said)
        self.assertIn("replay a past session with --offline", said)

    def test_with_json_a_refusal_goes_to_stderr_and_stdout_stays_json_or_nothing(self):
        """The US pack sends every refusal to stderr; a reader of --json must never get a line
        of prose where it expects the document."""
        for argv in (["orb", "--symbols", "SBIN", "--at", "2026-10-24 11:00"],
                     ["orb", "--symbols", "SBIN", "--at", "2026-10-22 09:20"],
                     ["orb", "--watchlist", str(self.home / "none.json"), "--at",
                      "2026-10-22 11:00"]):
            code, out, err = run_main_err(argv + ["--offline", str(self.home), "--json",
                                                  "--no-write"])
            self.assertEqual(out, "", argv)
            self.assertTrue(err.strip(), argv)
            self.assertIn(code, (1, 2))

    def test_a_turnover_file_with_no_figure_leaves_it_unknown_not_zero(self):
        import gzip
        import json
        body = json.loads(gzip.decompress((FIX / "exchange" / "bhavcopy.json.gz").read_bytes()))
        rows = body.get("data", body)["rows"]
        for row in rows:
            if row["symbol"] == "SBIN":
                row["turnover"] = None
        rows.append("not a row")
        (self.home / "exchange").mkdir()
        (self.home / "exchange" / "bhavcopy.json.gz").write_bytes(
            gzip.compress(json.dumps(body).encode("utf-8")))
        from nsetools import clock
        traded, day = scan.liquidity(clock.now("2026-10-26 11:00"), str(self.home))
        self.assertEqual(day, "2026-10-23")
        self.assertNotIn("SBIN", traded)
        self.assertIn("RELIANCE", traded)


@unittest.skipUnless(has(CACHE), "run capture.py")
class TheCachedScanSaysWhichDay(unittest.TestCase):
    """The cached scan prints the day of its closes, and calls a cache a session behind stale."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "p.sqlite")
        store, _ = price_cache(Path(self.db))
        self.last = store.trading_days()[-1]
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_day_is_said_and_a_stale_cache_is_called_stale(self):
        code, said = run_main(["--all", "--db", self.db, "--no-write", "--now",
                               "2026-10-26 08:00"])
        self.assertIn(f"Daily scan on the price cache, closes of {self.last}", said)
        self.assertNotIn("STALE", said)
        self.assertIn(f"symbols traded on {self.last}", said)
        self.assertNotIn("Nothing fired today", said)
        code, said = run_main(["--all", "--db", self.db, "--no-write", "--now",
                               "2026-10-28 08:00"])
        self.assertIn(f"STALE: the cache ends {self.last} and the session of 2026-10-27", said)
        import json
        code, said = run_main(["--all", "--db", self.db, "--no-write", "--json", "--now",
                               "2026-10-28 08:00"])
        self.assertIn("2026-10-27", json.loads(said)["stale"])
        code, said = run_main(["--all", "--db", self.db, "--no-write", "--json", "--now",
                               "2026-10-26 08:00"])
        self.assertNotIn("stale", json.loads(said))
        code, said = run_main(["--all", "--db", self.db, "--no-write", "--on", self.last,
                               "--now", "2026-10-28 08:00"])
        self.assertNotIn("STALE", said)


@unittest.skipUnless((FIX / "exchange" / "equities.json.gz").exists(), "run capture.py")
class TheUniverseAsTheUsPackWritesIt(unittest.TestCase):
    def test_one_symbol_and_name_a_line_offline_and_as_json(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "u.txt"
            code, said = run_main(["universe", "--offline", str(FIX), "--out", str(out)])
            lines = out.read_text(encoding="utf-8").splitlines()
            code2, said2 = run_main(["universe", "--offline", str(FIX), "--no-write", "--json"])
            read_back = scan.symbols_from_file(out)
        self.assertEqual(read_back[:1], ["20MICRONS"])
        self.assertEqual((code, code2), (0, 0))
        self.assertEqual(len(lines), 2359)
        self.assertEqual(lines[0], "20MICRONS|20 Microns Limited")
        self.assertIn("one SYMBOL|Name a line", said)
        self.assertEqual(json.loads(said2)["count"], 2359)


@unittest.skipUnless(has(CACHE), "run capture.py")
class TheCachedScanTakesTheSameFlags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "p.sqlite")
        store, _ = price_cache(Path(self.db))
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_named_list_limits_the_scan_and_json_is_the_document(self):
        import json
        code, said = run_main(["--all", "--db", self.db, "--no-write", "--json"])
        everyone = json.loads(said)
        some = sorted({h["symbol"] for h in everyone["hits"]})[:1]
        code, said = run_main(["--all", "--db", self.db, "--no-write", "--json",
                               "--symbols", ",".join(some)])
        doc = json.loads(said)
        self.assertEqual(sorted({h["symbol"] for h in doc["hits"]}), some)
        self.assertEqual((doc["mode"], doc["universe"]), ("daily", 1))
        self.assertIn("counts", doc)

    def test_a_day_outside_the_cache_and_a_damaged_cache_are_refused_plainly(self):
        code, said = run_main(["--all", "--db", self.db, "--on", "2099-01-01", "--no-write"])
        self.assertEqual(code, 2)
        self.assertIn("not in the cache", said)
        bad = Path(self.tmp.name) / "bad.sqlite"
        bad.write_text("garbage", encoding="utf-8")
        code, said = run_main(["--all", "--db", str(bad), "--no-write"])
        self.assertEqual(code, 2)
        self.assertIn("not a readable price cache", said)

    def test_no_write_with_save_writes_nothing(self):
        import os
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as empty:
            os.chdir(empty)
            try:
                run_main(["--all", "--db", self.db, "--no-write", "--save"])
                self.assertFalse((Path(empty) / "nse" / "scan.json").exists())
            finally:
                os.chdir(here)


if __name__ == "__main__":
    unittest.main(verbosity=2)
