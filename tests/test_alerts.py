"""Alerts: what rings, what does not, and what only rings once.

The plan is the real SBIN plan of 2026-10-22, the quote is a real saved response from the keyless
feed, and the fallback is the exchange's own closing file for 2026-10-23. The thresholds are the
prices that actually printed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import fixture, has, path, raw, skill                 # noqa: E402
from nsetools import clock                                       # noqa: E402

alerts = skill("nse-alerts", "alerts")

PLAN = "chain/plan.json"
BHAV = "exchange/bhavcopy.json.gz"
AT = clock.now("2026-10-22 11:00")


def plan():
    return fixture(PLAN)


@unittest.skipUnless(has(PLAN), "run capture.py")
class WhatRingsOnTheRealPlan(unittest.TestCase):
    """Every level below is the real stop or target of the real SBIN plan."""

    def setUp(self):
        self.plan = plan()
        self.stop = {"type": "stop", "symbol": "SBIN", "price": self.plan["stop"]}
        self.target = {"type": "target", "symbol": "SBIN", "price": self.plan["target"]}

    def test_the_plans_levels_are_the_real_ones(self):
        self.assertEqual(self.plan["stop"], 950.05)
        self.assertEqual(self.plan["target"], 985.45)

    def test_the_stop_rings_at_its_price_and_below(self):
        self.assertIsNotNone(alerts.rang(self.stop, 950.05, None, AT))
        self.assertIsNotNone(alerts.rang(self.stop, 949.00, None, AT))

    def test_the_stop_does_not_ring_a_tick_above(self):
        self.assertIsNone(alerts.rang(self.stop, 950.10, None, AT))

    def test_the_target_rings_at_its_price_and_above(self):
        self.assertIsNotNone(alerts.rang(self.target, 985.45, None, AT))
        self.assertIsNotNone(alerts.rang(self.target, 990.00, None, AT))

    def test_the_target_does_not_ring_a_tick_below(self):
        self.assertIsNone(alerts.rang(self.target, 985.40, None, AT))

    def test_the_real_entry_sits_between_the_two_so_neither_rings(self):
        """At the entry price the trade is open and nothing has happened yet."""
        self.assertIsNone(alerts.rang(self.stop, self.plan["entry"], None, AT))
        self.assertIsNone(alerts.rang(self.target, self.plan["entry"], None, AT))

    def test_a_missing_price_rings_nothing(self):
        self.assertIsNone(alerts.rang(self.stop, None, None, AT))


@unittest.skipUnless(has(BHAV), "run capture.py")
class PercentMovesOnRealCloses(unittest.TestCase):
    """Measured from the previous close in the exchange's own file."""

    def setUp(self):
        rows = fixture(BHAV)["rows"]
        self.row = max((r for r in rows if r.get("previous_close") and r.get("close")),
                       key=lambda r: abs(r["close"] - r["previous_close"]) / r["previous_close"])
        self.moved = ((self.row["close"] - self.row["previous_close"])
                      / self.row["previous_close"] * 100)

    def test_the_biggest_real_move_of_the_day_rings_a_smaller_threshold(self):
        rule = {"type": "pct_move", "symbol": self.row["symbol"],
                "pct": abs(self.moved) - 0.5}
        self.assertIsNotNone(alerts.rang(rule, self.row["close"],
                                         self.row["previous_close"], AT))

    def test_it_does_not_ring_a_threshold_above_it(self):
        rule = {"type": "pct_move", "symbol": self.row["symbol"],
                "pct": abs(self.moved) + 0.5}
        self.assertIsNone(alerts.rang(rule, self.row["close"],
                                      self.row["previous_close"], AT))

    def test_direction_is_respected(self):
        way = "down" if self.moved > 0 else "up"
        rule = {"type": "pct_move", "symbol": self.row["symbol"],
                "pct": abs(self.moved) - 0.5, "direction": way}
        self.assertIsNone(alerts.rang(rule, self.row["close"],
                                      self.row["previous_close"], AT))


class TheClockRules(unittest.TestCase):
    def test_a_time_rule_rings_once_the_clock_passes_it(self):
        rule = {"type": "time", "at": "15:20"}
        self.assertIsNone(alerts.rang(rule, None, None, clock.now("2026-10-22 15:19")))
        self.assertIsNotNone(alerts.rang(rule, None, None, clock.now("2026-10-22 15:20")))

    def test_the_opening_bell_rings_only_while_the_market_is_open(self):
        rule = {"type": "opening_bell"}
        self.assertIsNone(alerts.rang(rule, None, None, clock.now("2026-10-22 09:14")))
        self.assertIsNotNone(alerts.rang(rule, None, None, clock.now("2026-10-22 09:15")))

    def test_the_bell_does_not_ring_on_a_holiday(self):
        rule = {"type": "opening_bell"}
        self.assertIsNone(alerts.rang(rule, None, None, clock.now("2026-10-20 10:00")))


@unittest.skipUnless(has(PLAN), "run capture.py")
class ThreeAlertsFromTheRealPlan(unittest.TestCase):
    def test_the_stop_the_target_and_flat_by(self):
        made = alerts.rules_from_plan(plan())
        self.assertEqual([r["type"] for r in made], ["stop", "target", "time"])

    def test_the_levels_come_from_the_plan_not_from_anywhere_else(self):
        made = {r["type"]: r for r in alerts.rules_from_plan(plan())}
        self.assertEqual(made["stop"]["price"], plan()["stop"])
        self.assertEqual(made["target"]["price"], plan()["target"])
        self.assertEqual(made["time"]["at"], plan()["flat_by"])

    def test_every_rule_carries_the_plans_date(self):
        """So yesterday's stop never rings today against a price that has nothing to do with it."""
        for rule in alerts.rules_from_plan(plan()):
            self.assertEqual(rule["date"], "2026-10-22")

    def test_the_ids_are_stable_so_adding_twice_does_not_double_them(self):
        self.assertEqual([r["id"] for r in alerts.rules_from_plan(plan())],
                         [r["id"] for r in alerts.rules_from_plan(plan())])

    def test_a_plan_without_a_stop_is_refused(self):
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan(dict(plan(), stop=None))


@unittest.skipUnless(has(PLAN), "run capture.py")
class HowEachRuleRings(unittest.TestCase):
    """The US pack's rules for ringing, which this port follows, on the real plan's levels."""

    def walk(self, rule, prices):
        status = alerts.market(AT, offline=True)
        state, out = {}, []
        for price in prices:
            rings, state = alerts.evaluate([rule], {"SBIN": {"price": price}}, status, state, AT)
            out.append(bool(rings))
        return out

    def test_a_price_alert_rings_once_until_reset(self):
        target = plan()["target"]
        rule = {"id": "t", "type": "target", "symbol": "SBIN", "price": target}
        self.assertEqual(self.walk(rule, [target - 1, target + 1, target + 2, target - 5,
                                          target + 1]),
                         [False, True, False, False, False])

    def test_rearm_rings_again_only_after_the_price_crosses_back(self):
        """REGRESSION. A re-armed rule used to ring on EVERY check while past the level."""
        target = plan()["target"]
        rule = {"id": "t", "type": "target", "symbol": "SBIN", "price": target, "rearm": True}
        self.assertEqual(self.walk(rule, [target - 1, target + 1, target + 2, target - 5,
                                          target + 1, target + 3]),
                         [False, True, False, False, True, False])

    def test_a_time_alert_rings_only_on_a_trading_day(self):
        """REGRESSION. "Square off at 15:20" used to ring on a Sunday, and on Dussehra."""
        rule = {"id": "sq", "type": "time", "at": "15:20"}
        for when, rings in (("2026-10-25 15:25", False), ("2026-10-20 15:25", False),
                            ("2026-10-22 15:25", True)):
            at = clock.now(when)
            got, _ = alerts.evaluate([rule], {}, alerts.market(at, offline=True), {}, at)
            self.assertEqual(bool(got), rings, when)

    def test_a_time_alert_rings_once_a_day(self):
        at = clock.now("2026-10-22 15:25")
        status = alerts.market(at, offline=True)
        rule = {"id": "sq", "type": "time", "at": "15:20"}
        first, state = alerts.evaluate([rule], {}, status, {}, at)
        second, _ = alerts.evaluate([rule], {}, status, state, clock.now("2026-10-22 15:28"))
        self.assertTrue(first)
        self.assertFalse(second)

    def test_a_rule_dated_another_day_is_not_watched(self):
        rule = {"id": "s", "type": "stop", "symbol": "SBIN", "price": 999.0, "date": "2026-10-21"}
        self.assertEqual(self.walk(rule, [900.0]), [False])

    def test_a_bad_rule_is_refused(self):
        with self.assertRaises(alerts.AlertError):
            alerts.validate({"id": "x", "type": "stop", "symbol": "SBIN"})
        with self.assertRaises(alerts.AlertError):
            alerts.validate({"id": "x", "type": "time", "at": "25:99"})


class PricesAreMergedWithTheirOwnTimes(unittest.TestCase):
    def test_writing_one_price_keeps_the_others(self):
        import tempfile
        from pathlib import Path as P
        with tempfile.TemporaryDirectory() as tmp:
            where = P(tmp) / "prices.json"
            alerts.write_prices(where, {"SBIN": {"price": 958.6, "at": "2026-10-22T11:00:00+05:30"}},
                                AT, "feed")
            alerts.write_prices(where, {"ITC": {"price": 257.9, "at": "2026-10-22T11:01:00+05:30"}},
                                AT, "feed")
            import json as js
            doc = js.loads(where.read_text(encoding="utf-8"))
            self.assertEqual(set(doc["prices"]), {"SBIN", "ITC"})
            self.assertEqual(doc["detail"]["SBIN"]["at"], "2026-10-22T11:00:00+05:30")



@unittest.skipUnless(has("bars/SBIN-5m.json"), "run capture.py")
class QuotesFromARealResponse(unittest.TestCase):
    """The parser runs on the saved response, so the parser is what is being tested."""

    def test_a_real_response_yields_a_price_and_its_own_time(self):
        got = alerts.quote("SBIN", offline=str(path("quotes")))
        self.assertGreater(got["price"], 0)
        self.assertEqual(got["currency"], "INR")
        self.assertTrue(got["at"])

    def test_the_quote_says_which_source_it_came_from(self):
        """A fallback price must never be presented as a live one."""
        got = alerts.quote("SBIN", offline=str(path("quotes")))
        self.assertIn("source", got)


@unittest.skipUnless(has(BHAV), "run capture.py")
class TheExchangeFallback(unittest.TestCase):
    """When the intraday feed is down, the exchange's own closing file answers."""

    def setUp(self):
        self.real = alerts.nse if hasattr(alerts, "nse") else None
        rows = fixture(BHAV)
        import types
        from nsetools import nse as real_nse
        self.saved = real_nse.last_trading_day
        real_nse.last_trading_day = lambda *a, **k: (rows["day"], rows["rows"])
        self.nse = real_nse

    def tearDown(self):
        self.nse.last_trading_day = self.saved

    def test_it_finds_a_real_close_for_a_real_symbol(self):
        got = alerts.from_exchange("SBIN")
        rows = fixture(BHAV)["rows"]
        expected = next(r for r in rows if r["symbol"] == "SBIN")["close"]
        self.assertEqual(got["price"], round(float(expected), 2))

    def test_it_says_plainly_that_it_is_not_live(self):
        got = alerts.from_exchange("SBIN")
        self.assertIn("not a live price", got["source"])

    def test_a_symbol_the_exchange_did_not_list_raises(self):
        with self.assertRaises(alerts.NoQuote):
            alerts.from_exchange("NOSUCHCO")


class TheSideBySideGaps(unittest.TestCase):
    """Found comparing against the US pack, each measured before it was closed."""

    QUOTES = str(Path(__file__).resolve().parent / "fixtures" / "quotes")
    NOW = "2026-10-23 15:29"            # the saved SBIN quote is from 23 Oct 15:28 IST

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.rules, self.state = self.home / "rules.json", self.home / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, *argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = alerts.main(list(argv))
        return code, out.getvalue()

    def add(self, *argv):
        return self.run_("add", "--rules", str(self.rules), "--state", str(self.state), *argv)

    def check(self, quotes=None):
        return self.run_("check", "--rules", str(self.rules), "--state", str(self.state),
                         "--offline", quotes or self.QUOTES, "--now", self.NOW,
                         "--prices-out", str(self.home / "prices.json"))

    def test_a_damaged_rules_file_is_refused_not_overwritten(self):
        """Before: add wrote over it and the existing SBIN stop was lost."""
        damaged = '{"rules":[{"id":"x","type":"stop","symbol":"SBIN","price":900},]}'
        self.rules.write_text(damaged, encoding="utf-8")
        code, said = self.add("--symbol", "ITC", "--above", "300")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", said)
        self.assertEqual(self.rules.read_text(encoding="utf-8"), damaged)

    def test_a_damaged_state_file_does_not_ring_everything_again(self):
        self.add("--symbol", "SBIN", "--stop", "960")
        code, said = self.check()
        self.assertEqual(code, 0)
        self.assertIn("ALERT SBIN", said)
        self.state.write_text("{bad", encoding="utf-8")
        code, said = self.check()
        self.assertNotIn("ALERT", said)
        self.assertIn("not valid JSON", said)

    def test_a_ring_says_where_the_stop_is_and_shows_the_label(self):
        self.add("--symbol", "SBIN", "--stop", "960", "--label", "trail stop")
        code, said = self.check()
        self.assertIn("(trail stop)", said)
        self.assertIn("SL-M stop already sits at Zerodha", said)
        self.assertIn("/nse-broker flatten", said)

    def test_an_undated_quote_is_not_rung_on(self):
        import json
        quotes = self.home / "q"
        quotes.mkdir()
        body = json.loads((Path(self.QUOTES) / "ITC.json").read_text(encoding="utf-8"))
        body["chart"]["result"][0]["meta"].pop("regularMarketTime")
        (quotes / "ITC.json").write_text(json.dumps(body), encoding="utf-8")
        self.add("--symbol", "ITC", "--stop", "300")
        code, said = self.check(str(quotes))
        self.assertNotIn("ALERT", said)
        self.assertIn("carries no time", said)

    def test_a_high_priced_level_keeps_its_exact_id(self):
        code, said = self.add("--symbol", "MRF", "--stop", "140000.5")
        self.assertIn("mrf-stop-140000.50", said)
        code, said = self.add("--symbol", "MRF", "--stop", "140000.25")
        self.assertEqual(code, 0, said)

    def test_a_level_that_can_never_mean_anything_is_refused(self):
        for flag, value in (("--pct", "-3"), ("--pct", "0"), ("--stop", "-5")):
            code, said = self.add("--symbol", "INFY", flag, value)
            self.assertEqual(code, 1, (flag, value))

    def test_a_time_alert_before_the_open_does_not_say_closed(self):
        self.add("--at", "08:00", "--label", "square off")
        code, said = self.run_("check", "--rules", str(self.rules), "--state", str(self.state),
                               "--offline", self.QUOTES, "--now", "2026-10-22 08:30")
        self.assertIn("has not opened yet", said)
        self.assertNotIn("has closed", said)

    def test_a_broken_config_is_refused_not_replaced_by_defaults(self):
        """settings.load swallowed bad JSON, so every skill sized against the defaults."""
        from nsetools import settings
        bad = self.home / "config.json"
        bad.write_text("bad", encoding="utf-8")
        with self.assertRaises(settings.BadConfig):
            settings.load(bad)
        bad.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(settings.BadConfig):
            settings.load(bad)

    def test_clock_offline_is_a_plain_flag(self):
        code, said = self.run_("clock", "--offline", "--now", "2026-10-22 10:00")
        self.assertIn("NSE OPEN", said)

    def test_offline_never_downloads_and_a_bad_file_is_said(self):
        from nsetools import quotes
        with self.assertRaises(quotes.NoQuote):
            quotes.quote("SBIN", offline=self.QUOTES, source="exchange")
        (self.home / "SBIN.json").write_text("{oops", encoding="utf-8")
        with self.assertRaises(quotes.NoQuote):
            quotes.quote("SBIN", offline=str(self.home))
        with self.assertRaises(quotes.NoQuote) as said:
            quotes.quote("ZZZ", offline=str(self.home))
        self.assertIn("no offline quote file", str(said.exception))


@unittest.skipUnless(has("quotes/SBIN.json"), "run capture.py")
class TheThirdPass(unittest.TestCase):
    """Found by the third side-by-side pass. Every quote is the real saved SBIN or ITC response,
    copied and changed in the one fact under test."""

    QUOTES = Path(__file__).resolve().parent / "fixtures" / "quotes"
    NOW = "2026-10-23 15:29"            # the saved quotes are from 23 Oct 15:28 IST

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.rules, self.state = self.home / "rules.json", self.home / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def real(self, symbol="SBIN"):
        import json
        return json.loads((self.QUOTES / f"{symbol}.json").read_text(encoding="utf-8"))

    def saved(self, body, symbol="SBIN"):
        """A folder holding one changed copy of a real quote, for --offline."""
        import json
        folder = self.home / "q"
        folder.mkdir(exist_ok=True)
        (folder / f"{symbol}.json").write_text(json.dumps(body), encoding="utf-8")
        return str(folder)

    def run_(self, *argv, cwd=None):
        import contextlib
        import io
        import os
        out = io.StringIO()
        here = os.getcwd()
        try:
            if cwd:
                os.chdir(cwd)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = alerts.main(list(argv))
        finally:
            os.chdir(here)
        return code, out.getvalue()

    def add(self, *argv):
        return self.run_("add", "--rules", str(self.rules), "--state", str(self.state), *argv)

    def check(self, quotes=None, *more, now=None):
        return self.run_("check", "--rules", str(self.rules), "--state", str(self.state),
                         "--offline", quotes or str(self.QUOTES), "--now", now or self.NOW,
                         "--prices-out", str(self.home / "prices.json"), *more)

    # ---- quotes: nothing that is not a price is ever rung on

    def test_a_price_of_zero_negative_text_or_nan_is_refused(self):
        """Before: a price of 0 rang a stop at 960 as "SBIN is Rs 0.00", and text crashed."""
        from nsetools import quotes
        for bad in (0, -5, "958.6", float("nan"), True, None):
            body = self.real()
            body["chart"]["result"][0]["meta"]["regularMarketPrice"] = bad
            with self.assertRaises(quotes.NoQuote, msg=repr(bad)):
                quotes.quote("SBIN", offline=self.saved(body))

    def test_a_bad_previous_close_or_time_is_dropped_not_crashed_on(self):
        from nsetools import quotes
        body = self.real()
        meta = body["chart"]["result"][0]["meta"]
        meta["previousClose"], meta["regularMarketTime"] = "x", "x"
        got = quotes.quote("SBIN", offline=self.saved(body))
        self.assertEqual(got["previous_close"], meta["chartPreviousClose"])
        self.assertIsNone(got["at"])

    def test_the_quote_carries_the_days_open_high_and_low(self):
        from nsetools import quotes
        body = self.real()
        got = quotes.quote("SBIN", offline=str(self.QUOTES))
        first = next(x for x in body["chart"]["result"][0]["indicators"]["quote"][0]["open"]
                     if x is not None)
        self.assertEqual(got["open"], round(first, 2))
        self.assertEqual(got["high"], body["chart"]["result"][0]["meta"]["regularMarketDayHigh"])
        self.assertEqual(got["low"], body["chart"]["result"][0]["meta"]["regularMarketDayLow"])

    def test_pct_move_falls_back_to_the_days_open(self):
        """As the US pack does: no previous close, so the move is measured from the open."""
        body = self.real()
        meta = body["chart"]["result"][0]["meta"]
        meta.pop("previousClose")
        meta.pop("chartPreviousClose")
        self.add("--symbol", "SBIN", "--pct", "1")
        code, said = self.check(self.saved(body))
        self.assertEqual(code, 0, said)
        self.assertIn("(from Rs 969.65)", said)

    def test_a_feed_answer_with_no_result_is_said(self):
        from nsetools import quotes
        body = self.real()
        body["chart"]["result"] = []
        with self.assertRaises(quotes.NoQuote) as said:
            quotes.quote("SBIN", offline=self.saved(body))
        self.assertIn("no data for SBIN", str(said.exception))

    def test_an_offline_quote_may_be_gzipped(self):
        import gzip
        from nsetools import quotes
        folder = self.home / "gz"
        folder.mkdir()
        (folder / "SBIN.json.gz").write_bytes(gzip.compress((self.QUOTES / "SBIN.json")
                                                            .read_bytes()))
        self.assertEqual(quotes.quote("SBIN", offline=str(folder))["price"], 958.6)

    def test_a_429_waits_and_asks_once_more(self):
        """As the US pack does: one retry after a pause, on the same host."""
        import io
        import urllib.error
        from nsetools import quotes
        body = (self.QUOTES / "SBIN.json").read_bytes()
        calls, waits = [], []

        class Answer(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(request, timeout=None):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {},
                                             io.BytesIO(b""))
            return Answer(body)

        saved_open, saved_sleep = quotes.urllib.request.urlopen, quotes._time.sleep
        quotes.urllib.request.urlopen, quotes._time.sleep = fake, waits.append
        try:
            got = quotes.quote("SBIN")
        finally:
            quotes.urllib.request.urlopen, quotes._time.sleep = saved_open, saved_sleep
        self.assertEqual(got["price"], 958.6)
        self.assertEqual(waits, [quotes.BACKOFF])
        self.assertEqual(calls[0], calls[1])

    # ---- files and rules that cannot mean anything are refused, never crashed on

    def test_a_rules_file_of_the_wrong_shape_is_refused(self):
        for body in ('"abc"', "5", '{"rules": "x"}', '{"rules": [5]}'):
            self.rules.write_text(body, encoding="utf-8")
            code, said = self.check()
            self.assertEqual(code, 1, body)
            self.assertIn("alerts.py:", said, body)
            code, said = self.add("--symbol", "ITC", "--above", "300")
            self.assertEqual(code, 1, body)
            self.assertEqual(self.rules.read_text(encoding="utf-8"), body)

    def test_a_state_of_the_wrong_shape_does_not_ring_everything_again(self):
        """Before: a state of [] read as "nothing rang" and the SBIN stop rang again."""
        self.add("--symbol", "SBIN", "--stop", "960")
        self.assertEqual(self.check()[0], 0)
        for body in ("[]", '{"rung": []}', "5"):
            self.state.write_text(body, encoding="utf-8")
            code, said = self.check()
            self.assertNotIn("ALERT", said, body)
            self.assertIn("must hold", said, body)
            for cmd in (("list", "--rules", str(self.rules)), ("reset", "x")):
                code, said = self.run_(*cmd, "--state", str(self.state))
                self.assertEqual(code, 1)
                self.assertIn("must hold", said)

    def test_a_rule_that_cannot_mean_anything_is_refused(self):
        import json
        for rule in ({"type": "stop", "symbol": "SBIN", "price": True},
                     {"type": "stop", "symbol": "SBIN", "price": float("inf")},
                     {"type": "pct_move", "symbol": "SBIN", "pct": float("inf")},
                     {"type": "pct_move", "symbol": "SBIN", "pct": 1, "ref": "abc"},
                     {"type": "pct_move", "symbol": "SBIN", "pct": 1, "ref": -900},
                     {"type": "pct_move", "symbol": "SBIN", "pct": 1, "direction": "sideways"},
                     {"type": "stop", "symbol": "SBIN", "price": 900, "date": "2026-02-30"},
                     {"type": "stop", "symbol": 5, "price": 900},
                     {"id": 5, "type": "time", "at": "10:00"},
                     {"type": "time", "at": "10:00", "label": 5}):
            self.rules.write_text(json.dumps({"rules": [rule]}), encoding="utf-8")
            code, said = self.check()
            self.assertEqual(code, 1, rule)
            self.assertNotIn("Traceback", said)
            self.assertNotIn("ALERT", said, rule)

    def test_two_rules_under_one_id_are_refused(self):
        """The second could never ring once the first had: they share one "rang" mark."""
        import json
        self.rules.write_text(json.dumps({"rules": [
            {"type": "stop", "symbol": "SBIN", "price": 900},
            {"type": "stop", "symbol": "SBIN", "price": 960}]}), encoding="utf-8")
        code, said = self.check()
        self.assertEqual(code, 1)
        self.assertIn("more than one rule has the id SBIN-stop", said)

    def test_a_date_that_is_no_day_is_refused_at_add(self):
        code, said = self.add("--symbol", "SBIN", "--stop", "900", "--date", "2026-02-30")
        self.assertEqual(code, 1)
        self.assertFalse(self.rules.exists())

    def test_a_symbol_with_spaces_is_the_symbol(self):
        code, said = self.add("--symbol", " sbin ", "--stop", "960")
        self.assertIn('"symbol": "SBIN"', said)
        self.assertIn("ALERT SBIN", self.check()[1])

    @unittest.skipUnless(has(PLAN), "run capture.py")
    def test_a_plan_that_is_not_a_long_is_refused(self):
        """A short's stop sits above the price: its "at or below" alert would ring at once."""
        real = plan()
        for changed in (dict(real, side="sell"), dict(real, stop=real["target"] + 1),
                        dict(real, date="2026-02-30"), dict(real, stop=True)):
            with self.assertRaises(alerts.AlertError):
                alerts.rules_from_plan(changed)

    @unittest.skipUnless(has(PLAN), "run capture.py")
    def test_a_missing_config_file_is_said_not_a_traceback(self):
        import json
        saved = self.home / "plan.json"
        saved.write_text(json.dumps(plan()), encoding="utf-8")
        code, said = self.add("--from-plan", "--plan", str(saved), "--config",
                              str(self.home / "nope.json"))
        self.assertEqual(code, 1)
        self.assertIn("no config at", said)

    @unittest.skipUnless(has(PLAN), "run capture.py")
    def test_holdings_skip_a_row_that_is_not_a_position(self):
        import json
        folio = self.home / "portfolio.json"
        folio.write_text(json.dumps({"positions": [5, {"ticker": "ITC"}]}), encoding="utf-8")
        saved = self.home / "plan.json"
        saved.write_text(json.dumps(plan()), encoding="utf-8")
        self.assertEqual(alerts.holdings(folio, saved), ["ITC", "SBIN"])
        folio.write_text(json.dumps({"positions": {"a": 1}}), encoding="utf-8")
        self.assertEqual(alerts.holdings(folio, saved), ["SBIN"])

    def test_a_damaged_prices_file_is_rebuilt_not_crashed_on(self):
        import json
        where = self.home / "prices.json"
        where.write_text('{"prices": [1], "detail": 5}', encoding="utf-8")
        code, said = self.run_("quote", "SBIN", "--offline", str(self.QUOTES), "--write",
                               str(where))
        self.assertEqual(code, 0, said)
        self.assertEqual(json.loads(where.read_text(encoding="utf-8"))["prices"],
                         {"SBIN": 958.6})

    # ---- what the clock and the check say

    def test_the_clock_takes_an_iso_time_and_utc(self):
        self.assertEqual(clock.now("2026-10-22T04:30:00Z"), clock.now("2026-10-22 10:00"))
        self.assertEqual(clock.now("2026-10-22T10:00"), clock.now("2026-10-22 10:00"))
        with self.assertRaises(clock.BadTime):
            clock.now("22/10/2026 10:00")

    def test_the_market_says_what_the_us_pack_says(self):
        status = alerts.market(clock.now("2025-10-25 10:00"), offline=True)   # a Saturday
        self.assertEqual((status["reason"], status["calendar_known"]), ("weekend", False))
        status = alerts.market(clock.now("2026-10-20 10:00"), offline=True)   # Dussehra
        self.assertIsNone(status["closes"])
        status = alerts.market(clock.now("2026-10-22 10:00"), offline=True)   # open
        self.assertEqual(status["next_open"], "2026-10-23 09:15")

    def test_a_set_aside_price_is_not_listed_as_the_price_now(self):
        """Before: on a Saturday, "Nothing hit ... SBIN Rs 958.60" beside "not today"."""
        import json
        self.add("--symbol", "SBIN", "--stop", "900")
        code, said = self.check(now="2026-10-24 11:00")
        self.assertIn("not today", said)
        self.assertNotIn("SBIN Rs 958.60", said)
        code, said = self.check(None, "--json", now="2026-10-24 11:00")
        self.assertEqual(json.loads(said)["quotes"], {})

    def test_a_state_path_that_is_a_folder_is_said_not_a_traceback(self):
        folder = self.home / "a-folder"
        folder.mkdir()
        self.add("--symbol", "SBIN", "--stop", "960")
        code, said = self.run_("check", "--rules", str(self.rules), "--state", str(folder),
                               "--offline", str(self.QUOTES), "--now", self.NOW)
        self.assertEqual(code, 1)
        self.assertIn("cannot be read", said)


@unittest.skipUnless(has("quotes/SBIN.json"), "run capture.py")
class TheFourthPass(unittest.TestCase):
    """Found by the fourth side-by-side pass. Every quote is the real saved SBIN response, copied
    and changed in the one fact under test."""

    QUOTES = Path(__file__).resolve().parent / "fixtures" / "quotes"
    NOW = "2026-10-23 15:29"            # the saved quotes are from 23 Oct 15:28 IST

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.rules, self.state = self.home / "rules.json", self.home / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def real(self):
        import json
        return json.loads((self.QUOTES / "SBIN.json").read_text(encoding="utf-8"))

    def saved(self, body):
        import json
        folder = self.home / "q"
        folder.mkdir(exist_ok=True)
        (folder / "SBIN.json").write_text(json.dumps(body), encoding="utf-8")
        return str(folder)

    def run_(self, *argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = alerts.main(list(argv))
        return code, out.getvalue()

    def check(self, *more, prices=None):
        return self.run_("check", "--rules", str(self.rules), "--state", str(self.state),
                         "--offline", str(self.QUOTES), "--now", self.NOW,
                         "--prices-out", str(prices or self.home / "prices.json"), *more)

    def rules_are(self, *rules):
        import json
        self.rules.write_text(json.dumps({"rules": list(rules)}), encoding="utf-8")

    def test_an_answer_of_another_shape_is_refused_or_read_never_crashed_on(self):
        """Before: a result that was an object, indicators that were a list and an open that was
        a number each stopped the whole check with a traceback."""
        from nsetools import quotes
        body = self.real()
        body["chart"]["result"] = {"0": body["chart"]["result"][0]}
        with self.assertRaises(quotes.NoQuote):
            quotes.quote("SBIN", offline=self.saved(body))
        for change in (lambda r: r.update(indicators=[1]),
                       lambda r: r["indicators"]["quote"][0].update(open=5)):
            body = self.real()
            change(body["chart"]["result"][0])
            got = quotes.quote("SBIN", offline=self.saved(body))
            self.assertEqual((got["price"], got["open"]), (958.6, None))

    def test_a_damaged_gzip_body_is_said(self):
        """A gzip header over a damaged body raised zlib.error, which nothing caught."""
        import gzip
        from nsetools import quotes
        packed = gzip.compress((self.QUOTES / "SBIN.json").read_bytes())
        folder = self.home / "gz"
        folder.mkdir()
        (folder / "SBIN.json.gz").write_bytes(packed[:10] + b"\xff" * 40 + packed[50:])
        with self.assertRaises(quotes.NoQuote) as said:
            quotes.quote("SBIN", offline=str(folder))
        self.assertIn("not a readable gzip", str(said.exception))

    @unittest.skipUnless(has(BHAV), "run capture.py")
    def test_an_exchange_file_it_cannot_read_is_not_called_a_missing_symbol(self):
        """The exchange's other format has its own column names. "SBIN: not in the file" would
        be false: the file was not read. The rows are the real ones under the UDiFF names."""
        from nsetools import nse, quotes
        real = fixture(BHAV)
        renamed = [{"TckrSymb": r["symbol"], "ClsPric": r["close"]} for r in real["rows"]]
        saved, held = nse.last_trading_day, dict(quotes._EXCHANGE_FILE)
        nse.last_trading_day = lambda *a, **k: (real["day"], renamed)
        quotes._EXCHANGE_FILE.clear()
        try:
            with self.assertRaises(quotes.NoQuote) as said:
                quotes.from_exchange("SBIN")
        finally:
            nse.last_trading_day = saved
            quotes._EXCHANGE_FILE.clear()
            quotes._EXCHANGE_FILE.update(held)
        self.assertIn("shape this does not read", str(said.exception))
        self.assertNotIn("not in the exchange's file", str(said.exception))

    def test_an_empty_symbol_on_a_clock_rule_is_no_symbol(self):
        """As the US pack reads it. Before: the whole check was refused."""
        self.rules_are({"id": "ob", "type": "opening_bell", "symbol": ""})
        code, said = self.check()
        self.assertEqual(code, 0, said)
        self.assertIn("ALERT Opening bell", said)
        self.rules_are({"id": "s", "type": "stop", "symbol": " ", "price": 960})
        self.assertEqual(self.check()[0], 1)

    def test_a_rule_with_no_id_lists_by_the_id_it_rings_by(self):
        """Before: `list` crashed on a rule with no id, and reset could not be found from it."""
        self.rules_are({"type": "stop", "symbol": "SBIN", "price": 960})
        self.assertEqual(self.check()[0], 0)
        code, said = self.run_("list", "--rules", str(self.rules), "--state", str(self.state))
        self.assertEqual(code, 0, said)
        self.assertIn("SBIN-stop", said)
        self.assertIn("(rang 2026-10-23T15:29:00+05:30)", said)
        code, said = self.run_("add", "--rules", str(self.rules), "--state", str(self.state),
                               "--symbol", "SBIN", "--stop", "900", "--id", "SBIN-stop")
        self.assertEqual(code, 1)
        self.assertIn("already exists", said)
        self.assertEqual(self.run_("reset", "SBIN-stop", "--state", str(self.state))[0], 0)

    @unittest.skipUnless(has(PLAN), "run capture.py")
    def test_an_id_that_is_an_object_is_listed_and_kept_not_crashed_on(self):
        """Before: `list` and `add --from-plan` both crashed on {"id": {...}} in the file."""
        import json
        odd = {"id": {"a": 1}, "type": "stop", "symbol": "SBIN", "price": 900}
        self.rules_are(odd)
        code, said = self.run_("list", "--rules", str(self.rules), "--state", str(self.state))
        self.assertEqual(code, 0, said)
        saved = self.home / "plan.json"
        saved.write_text(json.dumps(plan()), encoding="utf-8")
        code, said = self.run_("add", "--from-plan", "--plan", str(saved), "--rules",
                               str(self.rules), "--state", str(self.state))
        self.assertEqual(code, 0, said)
        kept = json.loads(self.rules.read_text(encoding="utf-8"))["rules"]
        self.assertEqual(kept[0], odd)
        self.assertEqual(len(kept), 4)

    def test_a_prices_file_that_cannot_be_written_does_not_swallow_the_alert(self):
        """Before: the state marked the stop as rung, then the prices write failed and the run
        stopped, so the alert was never shown and never rang again."""
        self.rules_are({"id": "s", "type": "stop", "symbol": "SBIN", "price": 960})
        folder = self.home / "a-folder"
        folder.mkdir()
        code, said = self.check(prices=folder)
        self.assertEqual(code, 0, said)
        self.assertIn("ALERT SBIN is Rs 958.60", said)
        self.assertIn("the prices were not saved", said)
        code, said = self.run_("quote", "SBIN", "--offline", str(self.QUOTES), "--write",
                               str(folder))
        self.assertIn("SBIN       Rs 958.60", said)
        self.assertIn("the prices were not saved", said)
        self.assertNotIn("Wrote", said)

    def test_the_clock_line_always_carries_the_ist_time_and_the_day(self):
        """As the US pack's "New York 2026-10-19 15:55 (Monday): ..." does. Before 09:00 the line
        said only "NSE CLOSED." with no time and no day."""
        code, said = self.run_("clock", "--offline", "--now", "2026-10-23 08:30")
        self.assertTrue(said.startswith("IST 2026-10-23 08:30 (Friday): NSE CLOSED"), said)
        self.rules_are({"id": "ob", "type": "opening_bell"})
        self.assertIn("IST 2026-10-23 15:29 (Friday): NSE OPEN", self.check()[1])

    def test_a_weekend_in_a_year_with_no_list_still_says_the_list_is_unknown(self):
        code, said = self.run_("clock", "--offline", "--now", "2025-10-25 10:00")
        self.assertEqual(code, 1)
        self.assertIn("2025 NSE holiday list is not known", said)


if __name__ == "__main__":
    unittest.main(verbosity=2)
