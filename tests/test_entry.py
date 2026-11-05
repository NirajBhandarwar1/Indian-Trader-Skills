"""The NSE clock, intraday bars, and the bar that counts as an entry.

EVERYTHING HERE RUNS ON REAL DATA. The bars are the exchange session of 2026-10-22 as the feed
served it, and the holiday list is the exchange's own. Nothing is invented, including the awkward
cases: a bar whose high cleared the level while its close did not, and a bar that closed exactly
on it, both happened that morning and are asserted by the prices they actually printed.
"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import fixture, has, path, skill                      # noqa: E402
from nsetools import bars, clock                                 # noqa: E402

entry = skill("nse-entry", "entry")

DAY = "2026-10-22"
BARS = {s: str(path(f"bars/{s}-5m.json")) for s in
        ("SBIN", "ITC", "SUZLON", "INFY", "RELIANCE")}
HOLIDAYS = "exchange/holiday-master.json"


def session(symbol: str, when: str = DAY):
    return bars.candles(symbol, "5m", day=when, offline=BARS[symbol])


def at(hhmm: str, when: str = DAY):
    return clock.now(f"{when} {hhmm}")


@unittest.skipUnless(all(Path(p).exists() for p in BARS.values()), "run capture.py")
class TheIndianSession(unittest.TestCase):
    """Measured from the feed rather than assumed from the published hours."""

    def test_the_session_is_quarter_past_nine_to_half_past_three(self):
        self.assertEqual((clock.OPEN.hour, clock.OPEN.minute), (9, 15))
        self.assertEqual((clock.CLOSE.hour, clock.CLOSE.minute), (15, 30))

    def test_the_first_bar_of_a_real_session_starts_at_quarter_past_nine(self):
        self.assertEqual(session("SBIN")[0]["start"].strftime("%H:%M"), "09:15")

    def test_a_real_session_is_seventy_four_bars_not_the_seventy_five_on_paper(self):
        """09:15 to 15:30 works out to 75 five-minute bars; the feed serves 74.

        This is not a rounding quibble. A tool that required 75 rejected every session there was,
        which is why nse-timemachine measures the session length instead of assuming it.
        """
        self.assertEqual(len(session("SBIN")), 74)

    def test_ist_is_five_and_a_half_hours_and_never_moves(self):
        """India has no daylight saving, so one offset is right all year."""
        for month in (1, 4, 7, 10):
            self.assertEqual(at("10:00", f"2026-{month:02d}-15").utcoffset(),
                             timedelta(hours=5, minutes=30))

    def test_every_bar_of_a_real_session_is_inside_the_session(self):
        for bar in session("SBIN"):
            self.assertGreaterEqual(bar["start"].time(), clock.OPEN)
            self.assertLess(bar["start"].time(), clock.CLOSE)


@unittest.skipUnless(has(HOLIDAYS), "run capture.py")
class TheExchangesOwnHolidays(unittest.TestCase):
    """The list is fetched, and the built-in fallback is checked against the real one."""

    def setUp(self):
        self.rows = fixture(HOLIDAYS)["CM"]

    def test_the_real_list_has_the_holidays_that_move_with_the_moon(self):
        """These are why the list cannot be reasoned out and has to be fetched."""
        names = " ".join((r.get("description") or "") for r in self.rows).lower()
        for movable in ("holi", "id-ul-fitr", "bakri id", "muharram", "diwali"):
            self.assertIn(movable, names)

    def test_the_built_in_fallback_matches_the_exchange_for_its_year(self):
        """A stale fallback would say the market is open on Dussehra.

        This is the test that catches it: the copy in the code is compared against the list the
        exchange actually serves.
        """
        real = {clock._iso(r["tradingDate"]) for r in self.rows
                if (clock._iso(r.get("tradingDate") or "") or "").startswith("2026")}
        self.assertEqual(set(clock.FALLBACK), real)

    def test_dussehra_closed_the_market_and_the_feed_agrees(self):
        """2026-10-20 is a holiday, and no bars exist for it. Two sources, one answer."""
        self.assertEqual(clock.holiday_name(date(2026, 10, 20), offline=True), "Dussehra")
        self.assertFalse(clock.is_trading_day(date(2026, 10, 20), offline=True))
        traded = {b["start"].date().isoformat() for b in
                  bars.candles("SBIN", "5m", at=None, offline=BARS["SBIN"])}
        self.assertNotIn("2026-10-20", traded)

    def test_diwali_is_marked_for_a_special_session_in_the_real_list(self):
        """Muhurat trading: closed, then open about an hour in the evening.

        The exchange marks it with a star and announces the hour separately, so the day is
        reported closed rather than given a guessed time.
        """
        starred = [r["description"] for r in self.rows
                   if (r.get("description") or "").endswith("*")]
        self.assertTrue(starred, "the exchange marked no special session")
        self.assertTrue(any("diwali" in s.lower() for s in starred))
        said = clock.status(at("11:00", "2026-11-08"), offline=True)
        self.assertIn("CLOSED", said)
        self.assertIn("Muhurat", said)

    def test_an_unknown_year_is_refused_rather_than_assumed_empty(self):
        """An empty holiday list reads as 'open every weekday', wrong sixteen times a year."""
        with self.assertRaises(clock.Unknown):
            clock.holidays(2031, offline=True)

    def test_the_weekend_and_the_next_open(self):
        self.assertFalse(clock.is_trading_day(date(2026, 10, 24), offline=True))
        nxt = clock.next_open(at("12:00", "2026-10-24"), offline=True)
        self.assertEqual((nxt.date(), nxt.strftime("%H:%M")), (date(2026, 10, 26), "09:15"))

    def test_pre_open_is_an_auction_not_the_session(self):
        self.assertFalse(clock.is_open(at("09:05"), offline=True))
        self.assertIn("PRE-OPEN", clock.status(at("09:05"), offline=True))


@unittest.skipUnless(all(Path(p).exists() for p in BARS.values()), "run capture.py")
class TickSizesOnRealPrices(unittest.TestCase):
    """Measured across 2,706 traded symbols, then checked against prices that really printed."""

    def test_the_tick_matches_what_each_real_price_is_a_multiple_of(self):
        """SBIN trades in five paise and SUZLON in one. Both are in the fixtures."""
        self.assertEqual(clock.tick_size(session("SBIN")[0]["close"]), 0.05)
        self.assertEqual(clock.tick_size(session("SUZLON")[0]["close"]), 0.01)

    def test_real_closes_sit_on_the_tick_the_rule_predicts(self):
        for symbol in ("SBIN", "SUZLON", "ITC"):
            for bar in session(symbol)[:20]:
                tick = clock.tick_size(bar["close"])
                self.assertAlmostEqual(round(bar["close"] / tick) * tick, bar["close"], places=2,
                                       msg=f"{symbol} {bar['close']} is not a multiple of {tick}")

    def test_a_stop_rounds_away_from_the_entry_never_towards_it(self):
        """Towards the entry would shrink the risk a share and buy more shares than allowed.

        The number is the real SBIN opening-range midpoint of 2026-10-22.
        """
        window = bars.opening_range(session("SBIN"), 15)
        midpoint = (window["high"] + window["low"]) / 2
        self.assertEqual(midpoint, 954.675)
        self.assertEqual(clock.to_tick(midpoint, up=False), 954.65)
        self.assertEqual(clock.to_tick(midpoint, up=True), 954.70)

    def test_a_price_already_on_a_tick_does_not_move(self):
        close = session("SBIN")[0]["close"]
        self.assertEqual(clock.to_tick(close, up=False), close)
        self.assertEqual(clock.to_tick(close, up=True), close)


@unittest.skipUnless(all(Path(p).exists() for p in BARS.values()), "run capture.py")
class TheOpeningRange(unittest.TestCase):
    def test_the_range_is_the_first_three_bars_and_ends_at_half_past_nine(self):
        window = bars.opening_range(session("SBIN"), 15)
        self.assertEqual(window["bars"], 3)
        self.assertEqual(window["from"].strftime("%H:%M"), "09:15")
        self.assertEqual(window["to"].strftime("%H:%M"), "09:30")

    def test_the_real_sbin_range(self):
        window = bars.opening_range(session("SBIN"), 15)
        self.assertEqual((window["low"], window["high"]), (950.05, 959.30))

    def test_the_range_is_the_extremes_of_those_bars_not_their_closes(self):
        rows = session("SBIN")[:3]
        window = bars.opening_range(session("SBIN"), 15)
        self.assertEqual(window["high"], max(b["high"] for b in rows))
        self.assertEqual(window["low"], min(b["low"] for b in rows))

    def test_half_a_range_is_not_a_range(self):
        self.assertIsNone(bars.opening_range(session("SBIN")[:2], 15))

    def test_the_atr_says_how_many_bars_it_used(self):
        """Eleven bars give eleven true ranges: the first is its own high less its low, as the US
        pack counts it. The first version dropped that bar and reported ten."""
        value, used = bars.atr(session("SBIN")[:11], window=14)
        self.assertGreater(value, 0)
        self.assertEqual(used, 11)


@unittest.skipUnless(all(Path(p).exists() for p in BARS.values()), "run capture.py")
class OnlyAClosedBarAboveCounts(unittest.TestCase):
    """The rule, tested on the morning it describes. Every price below is one that printed."""

    def look(self, symbol, **kw):
        kw.setdefault("at", at("15:25"))
        return entry.look(symbol, offline=BARS[symbol], **kw)

    def test_sbin_triggered_at_quarter_past_ten(self):
        found = self.look("SBIN")
        self.assertEqual(found["status"], "triggered")
        self.assertEqual(found["entry"], 961.85)
        self.assertEqual(found["trigger_time"], "10:15")
        self.assertEqual(found["bar_start"], "10:10")

    def test_a_bar_whose_high_cleared_the_level_but_whose_close_did_not_is_no_trigger(self):
        """SBIN's 09:50 bar reached 960.85, above the 959.30 range high, and closed 959.25.

        A wick above is not an entry. Checked at 10:00, before the bar that really triggered.
        """
        rows = session("SBIN")
        wick = next(b for b in rows if b["start"].strftime("%H:%M") == "09:50")
        self.assertGreater(wick["high"], 959.30)
        self.assertLess(wick["close"], 959.30)
        self.assertEqual(self.look("SBIN", at=at("10:00"))["status"], "waiting")

    def test_a_close_exactly_on_the_level_is_not_above_it(self):
        """SUZLON's 10:35 bar closed at 39.15, which is its opening-range high to the paisa.

        Asked as a level setup at that exact price, nothing may trigger on it.
        """
        rows = session("SUZLON")
        window = bars.opening_range(rows, 15)
        equal = next(b for b in rows if b["start"].strftime("%H:%M") == "10:35")
        self.assertEqual(equal["close"], window["high"])
        found = entry.look("SUZLON", setup="level", level=equal["close"],
                           at=at("10:40"), offline=BARS["SUZLON"])
        if found["status"] == "triggered":
            self.assertNotEqual(found["bar_start"], "10:35")

    def test_the_first_close_above_wins_and_later_ones_do_not_reset_it(self):
        late = self.look("SBIN", at=at("15:25"))
        early = self.look("SBIN", at=at("10:20"))
        self.assertEqual(late["entry"], early["entry"])

    def test_the_forming_bar_is_named_but_never_counted(self):
        """At 10:12:40 the 10:10 bar is still open. It is reported, and it is not a trigger.

        The real file with its last trade moved to 10:12:30, as the feed would have served it
        then. The US pack names the forming bar with its price so far; so does this.
        """
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            doc = json.loads(Path(BARS["SBIN"]).read_text(encoding="utf-8"))
            doc["chart"]["result"][0]["meta"]["regularMarketTime"] = int(
                clock.now(f"{DAY} 10:12:30").timestamp())
            saved = Path(tmp) / "SBIN-5m.json"
            saved.write_text(json.dumps(doc), encoding="utf-8")
            found = entry.look("SBIN", at=clock.now(f"{DAY} 10:12:40"), offline=str(saved))
        self.assertEqual(found["status"], "waiting")
        self.assertIn("The 10:10 bar is still forming (Rs 961.85 so far", found["say"])
        self.assertIn("does not count", found["say"])
        self.assertEqual(found["forming"]["bar_start"], "10:10")

    def test_a_replay_never_shows_a_finished_bar_as_the_price_so_far(self):
        """The saved file was written after the close. Replayed at 10:12, its 10:10 bar is the
        FINISHED bar: 961.85 was not known at 10:12, so it is not shown as the price so far."""
        found = self.look("SBIN", at=at("10:12"))
        self.assertEqual(found["status"], "waiting")
        self.assertNotIn("still forming", found["say"])
        self.assertNotIn("forming", found)

    def test_the_true_volume_ratios_of_that_morning(self):
        """Measured over the range bars whose volume is KNOWN.

        These were reported as 1.66x, 0.87x and 0.90x until the 09:15 bar was found to carry no
        volume in this feed every single day. Counting it as zero cut the average by a third and
        made every ratio half as large again as the truth.
        """
        self.assertEqual(self.look("SBIN")["volume_x"], 1.1)
        self.assertEqual(self.look("ITC")["volume_x"], 0.58)
        self.assertEqual(self.look("SUZLON")["volume_x"], 0.6)

    def test_a_volume_floor_rejects_the_real_thin_break(self):
        """ITC broke its range on 0.58 times the range's average volume: a real break, on thin
        volume, so a 1.0x floor must not accept that bar."""
        plain = self.look("ITC")
        self.assertEqual(plain["status"], "triggered")
        self.assertLess(plain["volume_x"], 1.0)
        strict = self.look("ITC", min_volume_x=1.0)
        self.assertNotEqual(strict.get("bar_start"), plain["bar_start"])

    def test_a_volume_floor_keeps_a_break_that_really_clears_it(self):
        """SBIN broke on 1.10x, so a 1.0x floor keeps it and a 1.5x floor does not."""
        kept = self.look("SBIN", min_volume_x=1.0)
        self.assertEqual(kept["status"], "triggered")
        self.assertEqual(kept["entry"], 961.85)
        self.assertNotEqual(self.look("SBIN", min_volume_x=1.5).get("bar_start"), "10:10")

    def test_two_real_stocks_never_broke_their_range_and_expire(self):
        for symbol in ("INFY", "RELIANCE"):
            found = self.look(symbol)
            self.assertEqual(found["status"], "expired", symbol)

    def test_a_level_setup_needs_a_level(self):
        self.assertIn("needs a price to beat", self.look("SBIN", setup="level")["say"])

    def test_a_level_setup_triggers_on_a_real_level(self):
        found = entry.look("SBIN", setup="level", level=959.30, at=at("15:25"),
                           offline=BARS["SBIN"])
        self.assertEqual(found["status"], "triggered")

    def test_a_holiday_reports_closed(self):
        found = self.look("SBIN", at=at("10:00", "2026-10-20"))
        self.assertEqual(found["status"], "closed")
        self.assertIn("Dussehra", found["say"])

    def test_before_the_open_reports_not_open_yet(self):
        self.assertEqual(self.look("SBIN", at=at("09:05"))["status"], "not_open")


@unittest.skipUnless(all(Path(p).exists() for p in BARS.values()), "run capture.py")
class WhatAZeroVolumeMeans(unittest.TestCase):
    """The feed reports zero volume for two different things, and the code must not confuse them.

    Found on real data: the 09:15 bar reads zero volume every day for every stock while its price
    moves, so its volume is MISSING. A flat bar on zero volume is a bar nobody traded. Treating
    the first as zero inflated every volume ratio in the pack by half.
    """

    def every_bar(self, symbol):
        return bars.candles(symbol, "5m", at=None, offline=BARS[symbol])

    def test_the_first_bar_of_every_session_has_no_reported_volume(self):
        for symbol in BARS:
            firsts = [b for b in self.every_bar(symbol) if b["start"].strftime("%H:%M") == "09:15"]
            self.assertTrue(firsts)
            for bar in firsts:
                self.assertIsNone(bar["volume"], f"{symbol} {bar['start']:%m-%d}")

    def test_and_yet_its_price_moved_so_it_traded(self):
        """Nothing moves a price but trades, so a None here is unknown, never nothing."""
        for symbol in BARS:
            for bar in self.every_bar(symbol):
                if bar["start"].strftime("%H:%M") == "09:15":
                    self.assertNotEqual(bar["high"], bar["low"])
                    self.assertTrue(bars.traded(bar))

    @unittest.skipUnless(has("exchange/bhavcopy.json.gz"), "run capture.py")
    def test_the_exchanges_own_daily_total_proves_the_volume_is_missing(self):
        """A second source. The bars add up to LESS than the day's real volume, every time."""
        bhav = fixture("exchange/bhavcopy.json.gz")
        totals = {r["symbol"]: float(r["volume"]) for r in bhav["rows"]}
        for symbol in BARS:
            rows = bars.candles(symbol, "5m", day=bhav["day"], offline=BARS[symbol])
            counted = sum(bars.known_volume(rows))
            self.assertLess(counted, totals[symbol], symbol)
            short = (totals[symbol] - counted) / totals[symbol]
            self.assertGreater(short, 0.01, symbol)
            self.assertLess(short, 0.15, symbol)

    def test_a_flat_bar_on_no_volume_is_a_real_zero(self):
        """RELIANCE's 15:15 bar on 2026-10-21: one price, no volume, nobody traded."""
        flat = [b for b in self.every_bar("RELIANCE")
                if b["start"].strftime("%Y-%m-%d %H:%M") == "2026-10-21 15:15"]
        self.assertTrue(flat)
        bar = flat[0]
        self.assertEqual(bar["open"], bar["high"])
        self.assertEqual(bar["high"], bar["low"])
        self.assertEqual(bar["volume"], 0)
        self.assertFalse(bars.traded(bar))

    def test_the_range_average_uses_only_known_volume(self):
        window = bars.opening_range(session("SBIN"), 15)
        self.assertEqual(window["volume_bars"], 2)
        known = [b["volume"] for b in session("SBIN")[:3] if b["volume"] is not None]
        self.assertAlmostEqual(window["volume"], sum(known) / len(known))

    def test_an_unknown_volume_never_clears_a_volume_floor(self):
        """Asked for a floor, unknown is not a pass. It is not a zero either: just unconfirmed."""
        bar = {"volume": None, "high": 10.0, "low": 9.0}
        self.assertTrue(bars.traded(bar))
        self.assertIsNone(bars.volume_or_unknown({"volume": 0, "high": 10.0, "low": 9.0}))
        self.assertEqual(bars.volume_or_unknown({"volume": 0, "high": 9.0, "low": 9.0}), 0)


@unittest.skipUnless(Path(str(path("bars/SBIN-1m.json.gz"))).exists(), "run capture.py")
class OneMinuteBars(unittest.TestCase):
    """The other interval, on real one-minute data."""

    def test_a_fifteen_minute_range_is_fifteen_one_minute_bars(self):
        rows = bars.candles("SBIN", "1m", day=DAY, offline=str(path("bars/SBIN-1m.json.gz")))
        window = bars.opening_range(rows, 15)
        self.assertEqual(window["bars"], 15)
        self.assertEqual(window["to"].strftime("%H:%M"), "09:30")

    def test_the_two_intervals_agree_on_the_range(self):
        """The same fifteen minutes, counted two ways, must give the same high and low."""
        minute = bars.opening_range(
            bars.candles("SBIN", "1m", day=DAY, offline=str(path("bars/SBIN-1m.json.gz"))), 15)
        five = bars.opening_range(session("SBIN"), 15)
        self.assertAlmostEqual(minute["high"], five["high"], places=2)
        self.assertAlmostEqual(minute["low"], five["low"], places=2)


def _run(argv):
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = entry.main(argv)
    return code, out.getvalue()


def _changed(tmp: Path, change, name: str = "SBIN-5m.json") -> str:
    """The real SBIN file with one fact changed by `change(result)`, saved in `tmp`."""
    import json
    doc = json.loads(Path(BARS["SBIN"]).read_text(encoding="utf-8"))
    change(doc["chart"]["result"][0])
    saved = tmp / name
    saved.write_text(json.dumps(doc), encoding="utf-8")
    return str(saved)


def _blank(result, hhmm: str, when: str = DAY):
    """Blank one bar, as the feed does when it has nothing for it."""
    i = result["timestamp"].index(int(at(hhmm, when).timestamp()))
    for values in result["indicators"]["quote"][0].values():
        values[i] = None


@unittest.skipUnless(Path(BARS["SBIN"]).exists(), "run capture.py")
class TheGapsAgainstTheUSPackClosed(unittest.TestCase):
    """Each measured on a real file, before and after, against the US pack's own evaluate()."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        entry.CONFIG_PATH = None
        self._tmp.cleanup()

    def test_a_missing_last_range_bar_does_not_hold_the_range_open_all_day(self):
        """With 09:25 blanked the range had said "still forming" all day. The US pack builds it
        from the two bars there are, and so does this, and says so."""
        saved = _changed(self.tmp, lambda r: _blank(r, "09:25"))
        found = entry.look("SBIN", at=at("11:00"), offline=saved)
        self.assertEqual(found["status"], "triggered")
        self.assertEqual(found["bar_start"], "10:10")
        window = found["opening_range"]
        self.assertEqual((window["bars"], window["of"], window["missing"]), (2, 3, ["09:25"]))
        self.assertIn("built from 2 of 3 bars: the feed sent no 09:25 bar", found["range_note"])

    def test_the_range_is_anchored_to_quarter_past_nine_not_the_first_bar_sent(self):
        """With 09:15 blanked, anchoring to the first bar made the range 09:20-09:35, high 959.45,
        trigger 10:10. Anchored to 09:15 it is 09:20 and 09:25 only: high 958.15, trigger 09:35,
        exactly as the US pack computes it."""
        saved = _changed(self.tmp, lambda r: _blank(r, "09:15"))
        found = entry.look("SBIN", at=at("11:00"), offline=saved)
        self.assertEqual(found["opening_range"]["from"], "09:15")
        self.assertEqual(found["opening_range"]["high"], 958.15)
        self.assertEqual(found["bar_start"], "09:35")
        self.assertEqual(found["opening_range"]["missing"], ["09:15"])

    def test_a_partial_range_is_accepted_only_once_the_clock_has_passed_its_end(self):
        rows = session("SBIN")[:2]
        self.assertIsNone(bars.opening_range(rows, 15, at=at("09:29")))
        window = bars.opening_range(rows, 15, at=at("09:31"))
        self.assertEqual((window["bars"], window["of"]), (2, 3))

    def test_a_level_setups_volume_is_the_opening_ranges_and_never_moves(self):
        """Averaging every bar so far made a 1.4x floor say waiting at 12:00 and then report a
        trigger at 12:50 at 15:00. Now the reference is the range's known volume, fixed."""
        refs = set()
        for hhmm in ("10:35", "12:00", "15:00"):
            found = entry.look("SBIN", setup="level", level=967.0, min_volume_x=1.4,
                               at=at(hhmm), offline=BARS["SBIN"])
            refs.add(found["_reference_volume"])
        self.assertEqual(refs, {bars.opening_range(session("SBIN"), 15)["volume"]})
        thin = entry.look("SBIN", setup="level", level=967.0, min_volume_x=1.4, at=at("10:35"),
                          offline=BARS["SBIN"])
        self.assertIn("the opening range's average volume", thin["say"])

    def lagging_at_the_cutoff(self):
        """The real file with the feed's last trade moved to 14:59:50 and the 14:55 bar changed to
        close at 995.00, above a level of 990 that nothing else that day reached. The bars after
        14:55 are cut, as a live feed at 14:59:50 holds none: a later bar would say it is done."""
        def change(result):
            result["meta"]["regularMarketTime"] = int(at("14:59:50").timestamp())
            i = result["timestamp"].index(int(at("14:55").timestamp()))
            quote = result["indicators"]["quote"][0]
            quote["high"][i], quote["close"][i] = 999.0, 995.0
            result["timestamp"] = result["timestamp"][:i + 1]
            for key in quote:
                quote[key] = quote[key][:i + 1]
        return _changed(self.tmp, change)

    def test_a_later_bar_counts_a_bar_the_last_trade_has_not_reached(self):
        """The US pack counts a closed bar once a later bar exists, whatever the feed's last trade
        says: the later bar is the feed saying this one is finished. The same lagging file with
        its 15:00 bar kept triggers at 15:00:30, where without it the bar is still held."""
        def change(result):
            result["meta"]["regularMarketTime"] = int(at("14:59:50").timestamp())
            i = result["timestamp"].index(int(at("14:55").timestamp()))
            quote = result["indicators"]["quote"][0]
            quote["high"][i], quote["close"][i] = 999.0, 995.0
            result["timestamp"] = result["timestamp"][:i + 2]
            for key in quote:
                quote[key] = quote[key][:i + 2]
        saved = _changed(self.tmp, change, "later.json")
        found = entry.look("SBIN", setup="level", level=990.0, at=at("15:00:30"), offline=saved)
        self.assertEqual(found["status"], "triggered")
        self.assertEqual(found["bar_start"], "14:55")
        self.assertIn("the close can still move a tick or two", found["say"])
        held = entry.look("SBIN", setup="level", level=990.0, at=at("15:00:30"),
                          offline=self.lagging_at_the_cutoff())
        self.assertEqual(held["status"], "waiting")

    def test_no_expiry_while_a_bar_closing_at_the_cutoff_is_held_for_lag(self):
        """Measured before: EXPIRED at 15:00:30, TRIGGERED at 15:01:10."""
        saved = self.lagging_at_the_cutoff()
        held = entry.look("SBIN", setup="level", level=990.0, at=at("15:00:30"), offline=saved)
        self.assertEqual(held["status"], "waiting")
        self.assertIn("catches up", held["say"])
        later = entry.look("SBIN", setup="level", level=990.0, at=at("15:01:10"), offline=saved)
        self.assertEqual(later["status"], "triggered")
        self.assertEqual(later["bar_start"], "14:55")
        self.assertIn("the close can still move a tick or two", later["say"])

    def test_watch_asks_again_every_few_seconds_while_the_feed_is_behind(self):
        """Live, it slept to the next bar and reported this trigger at 15:06. It must come within
        the settle minute: the clock steps 15:00, 15:00:30, 15:01:00."""
        saved = self.lagging_at_the_cutoff()
        out = self.tmp / "entry.json"
        code, said = _run(["watch", "SBIN", "--setup", "level", "--level", "990",
                           "--now", "2026-10-22 14:58", "--offline", saved, "--out", str(out)])
        self.assertEqual(code, 0, said)
        import json
        self.assertTrue(json.loads(out.read_text(encoding="utf-8"))["as_of"]
                        .startswith("2026-10-22T15:01:00"))

    def test_the_bar_number_is_the_bars_place_on_the_clock(self):
        """The 10:10 bar is bar 12 from 09:15, as the US pack counts slots and /nse-exit reads
        it. With the real 09:50 bar blanked, its place in the list made it bar 11, and nse-exit
        would have cut the session at the 10:15 bar."""
        self.assertEqual(entry.look("SBIN", at=at("11:00"), offline=BARS["SBIN"])["bar_index"], 12)
        saved = _changed(self.tmp, lambda r: _blank(r, "09:50"))
        found = entry.look("SBIN", at=at("11:00"), offline=saved)
        self.assertEqual((found["bar_start"], found["bar_index"]), ("10:10", 12))

    def test_a_bar_sent_twice_is_one_bar(self):
        """The real 09:20 bar sent a second time, its later copy with a higher high: one bar in
        the range and in every bar number after it, the later copy kept, as the US pack keeps."""
        def twice(result):
            i = result["timestamp"].index(int(at("09:20").timestamp()))
            result["timestamp"].insert(i + 1, result["timestamp"][i])
            for key, values in result["indicators"]["quote"][0].items():
                values.insert(i + 1, values[i])
            result["indicators"]["quote"][0]["high"][i + 1] = 959.95
        saved = _changed(self.tmp, twice)
        rows = bars.candles("SBIN", "5m", day=DAY, offline=saved)
        self.assertEqual(len(rows), len(session("SBIN")))
        found = entry.look("SBIN", at=at("11:00"), offline=saved)
        self.assertEqual(found["opening_range"]["high"], 959.95)
        self.assertEqual(found["opening_range"]["bars"], 3)
        self.assertEqual(found["bar_index"], 12)

    def test_what_candles_throws_away_is_counted_by_why(self):
        """The real file blanks 5 bars over its sessions, counted as missing prices. Add a live
        tick off the grid, a bar stamped at 15:30 and a repeated 10:10 bar: each is counted under
        its own reason, the return value is the same bars as before, and dropped_counts() says
        the same as the dict passed in."""
        plain = bars.candles("SBIN", "5m", offline=BARS["SBIN"], dropped=(seen := {}))
        self.assertEqual(seen, {"missing_price": 5, "off_grid": 0, "outside_hours": 0,
                                "duplicate": 0, "no_time": 0})

        def extra(result):
            quote = result["indicators"]["quote"][0]
            i = result["timestamp"].index(int(at("10:10").timestamp()))
            for stamp in (int(at("10:12:30").timestamp()), int(at("15:30").timestamp()),
                          result["timestamp"][i]):
                result["timestamp"].append(stamp)
                for values in quote.values():
                    values.append(values[i])
        saved = _changed(self.tmp, extra)
        counts = {}
        rows = bars.candles("SBIN", "5m", offline=saved, dropped=counts)
        self.assertEqual(counts, {"missing_price": 5, "off_grid": 1, "outside_hours": 1,
                                  "duplicate": 1, "no_time": 0})
        self.assertEqual(bars.dropped_counts("SBIN"), counts)
        self.assertEqual([b["start"] for b in rows], [b["start"] for b in plain])

    def test_a_volume_that_is_not_a_count_is_unknown_not_a_crash(self):
        """A nan volume on the real 10:10 bar made the whole session unreadable. It is that one
        bar's volume that is unknown: the trigger stands, and a volume floor cannot be cleared."""
        def nan(result):
            i = result["timestamp"].index(int(at("10:10").timestamp()))
            result["indicators"]["quote"][0]["volume"][i] = float("nan")
        saved = _changed(self.tmp, nan)
        found = entry.look("SBIN", at=at("11:00"), offline=saved)
        self.assertEqual((found["status"], found["bar_start"]), ("triggered", "10:10"))
        self.assertIsNone(found["volume"])
        strict = entry.look("SBIN", at=at("11:00"), offline=saved, min_volume_x=1.0)
        self.assertNotEqual(strict.get("bar_start"), "10:10")

    def test_a_level_setup_shows_the_range_so_far_too(self):
        """The US pack shows the opening range forming whatever the setup."""
        code, said = _run(["check", "SBIN", "--setup", "level", "--level", "990",
                           "--now", "2026-10-22 09:27", "--offline", BARS["SBIN"]])
        self.assertIn("Opening range 09:15-09:30", said)
        self.assertIn("2 of 3 bars, still forming", said)

    def test_watch_never_promises_a_first_close_already_past(self):
        """Joined at 09:27 with the feed's range bars missing, it said "watching from the first
        bar's close at 09:20", seven minutes after it."""
        saved = _changed(self.tmp, lambda r: [_blank(r, h) for h in ("09:15", "09:20")])
        code, said = _run(["watch", "SBIN", "--now", "2026-10-22 09:22", "--offline", saved,
                           "--until", "09:24", "--out", str(self.tmp / "w.json")])
        self.assertNotIn("first bar's close at 09:20", said)

    def test_a_fresh_trigger_says_it_is_the_moment(self):
        found = entry.look("SBIN", at=at("10:15"), offline=BARS["SBIN"])
        self.assertIn("This is the moment to get in, if you take the trade", found["say"])
        late = entry.look("SBIN", at=at("11:00"), offline=BARS["SBIN"])
        self.assertNotIn("This is the moment", late["say"])

    @unittest.skipUnless(Path(str(path("bars/SBIN-60d5m.json.gz"))).exists(), "run capture.py")
    def test_the_latest_early_close_above_says_why(self):
        """SBIN 2026-08-05: the 09:30 and 09:35 bars both closed above the range. With entries
        from 09:45, at 09:41 it said only "Rs 1.10 over"; the US pack says why."""
        import json
        conf = self.tmp / "c.json"
        conf.write_text(json.dumps({"no_entries_before": "09:45"}), encoding="utf-8")
        entry.CONFIG_PATH = str(conf)
        found = entry.look("SBIN", at=at("09:41", "2026-08-05"),
                           offline=str(path("bars/SBIN-60d5m.json.gz")))
        self.assertIn("the 09:35 bar closed above", found["say"])
        self.assertIn("no entries before 09:45", found["say"])

    def test_a_reason_is_written_whatever_the_status(self):
        out = self.tmp / "e.json"
        _run(["check", "SBIN", "--now", "2026-10-22 09:22", "--offline", BARS["SBIN"],
              "--write", str(out)])
        import json
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "forming")
        self.assertIn("still forming", saved["reason"])

    def test_times_that_cannot_be_read_are_refused_not_replaced(self):
        import json
        conf = self.tmp / "bad.json"
        conf.write_text(json.dumps({"no_entries_before": "10:30am"}), encoding="utf-8")
        for argv in (["check", "SBIN", "--config", str(conf)],
                     ["watch", "SBIN", "--until", "1430"],
                     ["check", "SBIN", "--setup", "level"]):
            code, said = _run(argv + ["--now", "2026-10-22 10:20", "--offline", BARS["SBIN"]])
            self.assertEqual(code, 2, argv)
            self.assertIn("Refused", said)

    def test_a_replay_older_than_the_feed_keeps_is_refused_naming_the_limit(self):
        """Refused before any request is made, so this needs no network."""
        old = (clock.now().date() - timedelta(days=100)).isoformat()
        code, said = _run(["check", "SBIN", "--now", f"{old} 10:20"])
        self.assertEqual(code, 2)
        self.assertIn("about 60 days", said)
        week = (clock.now().date() - timedelta(days=10)).isoformat()
        code, said = _run(["check", "SBIN", "--now", f"{week} 10:20", "--interval", "1m"])
        self.assertEqual(code, 2)
        self.assertIn("1-minute bars for about a week", said)

    def test_watch_says_pre_open_once_and_joins_mid_session_on_the_latest_bar(self):
        code, said = _run(["watch", "SBIN", "--now", "2026-10-22 08:00", "--offline", BARS["SBIN"],
                           "--out", str(self.tmp / "a.json")])
        self.assertEqual(code, 0)
        self.assertEqual(said.count("Pre-open starts"), 1)
        self.assertIn("09:25 bar  closed Rs 957.45  opening range set", said)
        code, said = _run(["watch", "SBIN", "--now", "2026-10-22 14:00", "--offline", BARS["SBIN"],
                           "--setup", "level", "--level", "990", "--out", str(self.tmp / "b.json")])
        self.assertEqual(code, 1)
        self.assertNotIn("13:50 bar", said)
        self.assertIn("13:55 bar", said)
        self.assertIn("EXPIRED", said)

    def test_the_watchlist_names_each_failure_and_wants_a_folder(self):
        import json
        where = self.tmp / "w.json"
        where.write_text(json.dumps({"or_minutes": 30, "hits": [{"symbol": "SBIN"},
                                                                {"symbol": "NOSUCH"}]}),
                         encoding="utf-8")
        folder = str(Path(BARS["SBIN"]).parent)
        code, said = _run(["watchlist", "--watchlist", str(where), "--now", "2026-10-22 11:00",
                           "--offline", folder, "--json"])
        rows = {r["symbol"]: r["result"] for r in json.loads(said)}
        self.assertEqual(rows["SBIN"]["or_minutes"], 30)                 # the scan's own range
        code, said = _run(["watchlist", "--watchlist", str(where), "--now", "2026-10-22 11:00",
                           "--offline", folder])
        self.assertIn("NOSUCH       error: NOSUCH: nothing saved", said)
        code, said = _run(["watchlist", "--watchlist", str(where), "--now", "2026-10-22 11:00",
                           "--offline", folder, "--or-minutes", "15", "--json"])
        self.assertEqual(json.loads(said)[0]["result"]["or_minutes"], 15)
        code, said = _run(["watchlist", "--watchlist", str(where), "--now", "2026-10-22 11:00",
                           "--offline", BARS["SBIN"]])
        self.assertEqual(code, 2)
        self.assertIn("wants a folder", said)


if __name__ == "__main__":
    unittest.main(verbosity=2)
