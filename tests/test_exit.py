"""The stop and the target: where they go, how they round, and when they are refused.

Runs on the real SBIN trigger of 2026-10-22 and the real opening range that produced it, loaded
from the saved entry.json a real run of /nse-entry wrote. The ATR stop runs on that session's own
bars.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import fixture, has, path, skill                      # noqa: E402
from nsetools import bars                                        # noqa: E402

planner = skill("nse-exit", "exit")

ENTRY = "chain/entry.json"
SBIN_BARS = str(path("bars/SBIN-5m.json"))
SUZLON_BARS = str(path("bars/SUZLON-5m.json"))


def real():
    """The trigger a real run of /nse-entry recorded."""
    return fixture(ENTRY)


@unittest.skipUnless(has(ENTRY), "run capture.py")
class WhereTheStopGoes(unittest.TestCase):
    def setUp(self):
        self.entry = real()
        self.window = self.entry["opening_range"]
        self.price = self.entry["entry"]

    def test_the_real_trigger_is_what_the_plan_is_built_on(self):
        self.assertEqual(self.entry["symbol"], "SBIN")
        self.assertEqual(self.price, 961.85)
        self.assertEqual((self.window["low"], self.window["high"]), (950.05, 959.30))

    def test_the_midpoint_is_the_default(self):
        """(959.30 + 950.05) / 2 = 954.675, down to the five-paise tick SBIN trades in."""
        plan = planner.make("SBIN", self.price, window=self.window)
        self.assertEqual(plan["stop"], 954.65)
        self.assertIn("midpoint", plan["stop_why"])

    def test_the_range_low_is_wider_and_risks_more_a_share(self):
        wide = planner.make("SBIN", self.price, stop_rule="or-low", window=self.window)
        narrow = planner.make("SBIN", self.price, window=self.window)
        self.assertEqual(wide["stop"], 950.05)
        self.assertGreater(wide["risk_per_share"], narrow["risk_per_share"])

    def test_a_price_you_name(self):
        self.assertEqual(planner.make("SBIN", self.price, stop_rule="price:955")["stop"], 955.00)

    def test_a_percent_under_the_entry(self):
        plan = planner.make("SBIN", self.price, stop_rule="pct:2")
        self.assertAlmostEqual(plan["stop"], 942.60, places=2)

    def test_an_atr_stop_runs_on_the_sessions_own_bars(self):
        """The ATR is measured from the real session, not supplied."""
        plan = planner.make("SBIN", self.price, stop_rule="atr:1.5", window=self.window,
                            day="2026-10-22", offline=SBIN_BARS)
        measured = bars.atr(bars.candles("SBIN", "5m", day="2026-10-22", offline=SBIN_BARS))
        self.assertLess(plan["stop"], self.price)
        self.assertIn("ATR", plan["stop_why"])
        self.assertAlmostEqual(self.price - plan["stop"], 1.5 * measured[0], delta=0.05)

    def test_an_opening_range_stop_needs_an_opening_range(self):
        with self.assertRaises(planner.Refused):
            planner.make("SBIN", self.price, stop_rule="or-mid", window=None)

    def test_an_unknown_rule_is_refused(self):
        with self.assertRaises(planner.Refused):
            planner.make("SBIN", self.price, stop_rule="vibes")


@unittest.skipUnless(has(ENTRY), "run capture.py")
class WhereTheTargetGoes(unittest.TestCase):
    def setUp(self):
        self.entry = real()
        self.window = self.entry["opening_range"]
        self.price = self.entry["entry"]

    def test_two_r_is_the_default(self):
        plan = planner.make("SBIN", self.price, window=self.window)
        self.assertEqual(plan["risk_per_share"], 7.20)
        self.assertEqual(plan["target"], 976.25)
        self.assertEqual(plan["reward_risk"], 2.0)

    def test_one_r(self):
        plan = planner.make("SBIN", self.price, target_rule="1R", window=self.window)
        self.assertEqual(plan["reward_risk"], 1.0)

    def test_a_price_you_name_rounds_up_to_a_tick(self):
        plan = planner.make("SBIN", self.price, target_rule="price:1000.02",
                            window=self.window)
        self.assertEqual(plan["target"], 1000.05)


@unittest.skipUnless(has(ENTRY), "run capture.py")
class RefusedWithTheReason(unittest.TestCase):
    def setUp(self):
        self.price = real()["entry"]
        self.window = real()["opening_range"]

    def test_a_stop_above_the_entry_is_not_a_stop(self):
        with self.assertRaises(planner.Refused) as caught:
            planner.make("SBIN", self.price, stop_rule="price:970")
        self.assertIn("above the entry", str(caught.exception))

    def test_a_target_at_or_below_the_entry_is_refused(self):
        with self.assertRaises(planner.Refused) as caught:
            planner.make("SBIN", self.price, target_rule="price:900", window=self.window)
        # The reason, not a story: "the move has already happened" was said of a target the
        # user had simply typed below the entry.
        self.assertIn("at or below the entry", str(caught.exception))
        self.assertNotIn("already happened", str(caught.exception))

    def test_a_stop_too_tight_for_the_spread_is_refused(self):
        with self.assertRaises(planner.Refused) as caught:
            planner.make("SBIN", self.price, stop_rule="price:961.80")
        self.assertIn("too tight", str(caught.exception))

    def test_the_floor_is_two_ticks_on_a_share_that_trades_in_paise(self):
        """SUZLON really closed at 39.18 that session, and 0.05% of it is under two paise.

        A percentage floor alone would allow a stop one paisa under the entry, which is a stop
        sitting on the entry. The real price is why the tick floor exists.
        """
        rows = bars.candles("SUZLON", "5m", day="2026-10-22", offline=SUZLON_BARS)
        price = next(b["close"] for b in rows if b["start"].strftime("%H:%M") == "10:30")
        self.assertEqual(price, 39.18)
        self.assertLess(price * planner.MIN_RISK_PCT, 0.02)
        with self.assertRaises(planner.Refused):
            planner.make("SUZLON", price, stop_rule="price:39.17")
        self.assertEqual(planner.make("SUZLON", price, stop_rule="price:39.00")["stop"], 39.00)

    def test_an_entry_after_the_flat_by_time_is_refused(self):
        with self.assertRaises(planner.Refused) as caught:
            planner.make("SBIN", self.price, window=self.window, entry_time="15:25",
                         flat_by="15:20")
        self.assertIn("flat-by", str(caught.exception))


@unittest.skipUnless(has(ENTRY), "run capture.py")
class WhatThePlanCarries(unittest.TestCase):
    def test_everything_the_next_skill_needs(self):
        got = real()
        plan = planner.make("SBIN", got["entry"], window=got["opening_range"],
                            day=got["date"], entry_time=got["trigger_time"], setup="orb")
        for key in ("symbol", "side", "entry", "stop", "target", "risk_per_share",
                    "reward_risk", "flat_by", "date", "currency"):
            self.assertIn(key, plan)
        self.assertEqual(plan["side"], "buy")
        self.assertEqual(plan["currency"], "INR")
        self.assertEqual(plan["date"], "2026-10-22")

    def test_the_default_flat_by_leaves_room_before_the_close(self):
        """15:20 is the broker square-off convention; the exchange closes 15:30."""
        self.assertEqual(planner.make("SBIN", real()["entry"],
                                      window=real()["opening_range"])["flat_by"], "15:20")

    def test_a_tight_stop_warns_about_the_gate_rather_than_duplicating_it(self):
        """The real midpoint stop is 0.75% away, under the 1% floor /nse-risk enforces.

        The warning exists so that is known when the plan is made, not three steps later. It is a
        warning and not a refusal, because the floor belongs to one skill only.
        """
        plan = planner.make("SBIN", real()["entry"], window=real()["opening_range"])
        self.assertLess(plan["risk_percent"], 1.0)
        self.assertIsNotNone(plan["warning"])
        self.assertIn("nse-risk", plan["warning"])

    def test_a_wide_enough_stop_carries_no_warning(self):
        plan = planner.make("SBIN", real()["entry"], stop_rule="or-low",
                            window=real()["opening_range"])
        self.assertGreater(plan["risk_percent"], 1.0)
        self.assertIsNone(plan["warning"])


@unittest.skipUnless(has(ENTRY), "run capture.py")
class NothingFromAfterTheEntry(unittest.TestCase):
    """REGRESSION. An ATR stop measured on bars that had not happened yet when the trade was on."""

    def setUp(self):
        self.entry = real()
        self.rows = bars.candles("SBIN", "5m", day="2026-10-22", offline=SBIN_BARS)

    def test_the_atr_uses_only_bars_up_to_the_entry_bar(self):
        """The SBIN entry was bar 12, so its ATR uses those 12 bars and nothing after them.

        The first version used the whole session, so a 10:15 stop was sized on volatility from
        hours later and came out far too tight.
        """
        known = bars.atr(self.rows[:12])
        later = bars.atr(self.rows)
        self.assertNotAlmostEqual(known[0], later[0], places=1)
        plan = planner.make("SBIN", 961.85, stop_rule="atr:1.5", window=self.entry["opening_range"],
                            day="2026-10-22", offline=SBIN_BARS, entry_bar=12,
                            entry_time="10:15")
        self.assertAlmostEqual(961.85 - plan["stop"], 1.5 * known[0], delta=0.05)
        self.assertIn("12-bar ATR", plan["stop_why"])

    def test_the_entry_time_cuts_the_bars_when_there_is_no_bar_number(self):
        cut = planner.bars_to_entry("SBIN", "5m", "2026-10-22", SBIN_BARS, None, "10:15")
        self.assertEqual(cut[-1]["end"].strftime("%H:%M"), "10:15")
        self.assertTrue(all(b["end"].strftime("%H:%M") <= "10:15" for b in cut))

    def test_too_few_bars_before_the_entry_is_refused(self):
        with self.assertRaises(planner.Refused):
            planner.make("SBIN", 961.85, stop_rule="atr:1.5", day="2026-10-22",
                         offline=SBIN_BARS, entry_bar=2)


@unittest.skipUnless(has(ENTRY), "run capture.py")
class APlanMadeByHand(unittest.TestCase):
    def setUp(self):
        from nsetools import clock
        self.at = clock.now("2026-10-22 11:00")

    def test_it_carries_todays_date_so_a_stale_plan_is_caught(self):
        """REGRESSION. A hand-made plan used to carry no date, so /nse-budget could not tell it
        was from another day."""
        plan = planner.make("SBIN", 961.85, window=real()['opening_range'], at=self.at)
        self.assertEqual(plan["date"], "2026-10-22")

    def test_a_plan_for_another_day_says_so(self):
        plan = planner.make("SBIN", 961.85, window=real()['opening_range'], day="2026-10-21", at=self.at)
        self.assertTrue(any("not today" in w for w in plan["warnings"]))

    def test_a_level_setup_needs_a_stop_named(self):
        with self.assertRaises(planner.Refused) as caught:
            planner.make("SBIN", 961.85, setup="level", at=self.at)
        self.assertIn("level setup", str(caught.exception))

    def test_the_opening_range_is_kept_with_its_length(self):
        plan = planner.make("SBIN", 961.85, window=dict(real()['opening_range'], minutes=15), at=self.at)
        self.assertEqual(plan["opening_range"], {"high": 959.30, "low": 950.05, "minutes": 15})

    def test_it_records_who_made_it_and_when(self):
        plan = planner.make("SBIN", 961.85, window=real()['opening_range'], at=self.at)
        self.assertEqual(plan["created_by"], "nse-exit")
        self.assertTrue(plan["created_at"])


import contextlib                                                 # noqa: E402
import io                                                         # noqa: E402
import json                                                       # noqa: E402
import tempfile                                                   # noqa: E402

from nsetools import clock                                        # noqa: E402

PLAN = "chain/plan.json"
EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.json"
AT = "2026-10-22 11:00"


def cli(*argv):
    """Run exit.py's main in-process: (exit code, everything it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = planner.main(list(argv))
        finally:
            planner.CONFIG_PATH = None
    return code, out.getvalue()


class _Files(unittest.TestCase):
    """Each test writes a COPY of a real saved file with one fact changed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, planner, "CONFIG_PATH", None)

    def write(self, name, body):
        where = Path(self.tmp.name) / name
        where.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return str(where)

    def config(self, **changed):
        """The real example config with one rule changed (None removes it)."""
        rules = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        for key, value in changed.items():
            if value is None:
                rules.pop(key, None)
            else:
                rules[key] = value
        return self.write("config.json", rules)

    def refused(self, *argv):
        code, said = cli(*argv)
        self.assertEqual(code, 2, said)
        self.assertIn("Refused", said)
        self.assertNotIn("Traceback", said)
        return said


@unittest.skipUnless(has(ENTRY), "run capture.py")
class TheEntryBarCountsFromOne(_Files):
    """REGRESSION. Bar 0 read as 'no bar' and used the whole session; -1 dropped the last bar."""

    def test_bar_zero_is_refused_not_read_as_the_whole_session(self):
        for bad in (0, -1):
            with self.assertRaises(planner.Refused) as caught:
                planner.make("SBIN", 961.85, stop_rule="atr:1.5", day="2026-10-22",
                             offline=SBIN_BARS, entry_bar=bad, at=clock.now(AT))
            self.assertIn("counts from 1", str(caught.exception))
        said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop", "atr:1.5",
                            "--offline", SBIN_BARS, "--date", "2026-10-22", "--entry-bar", "0",
                            "--now", AT)
        self.assertIn("counts from 1", said)


@unittest.skipUnless(has(ENTRY), "run capture.py")
class TheConfigRules(_Files):
    def test_min_reward_risk_defaults_to_one_as_nse_risk_does(self):
        """REGRESSION. With no min_reward_risk set, a 0.5R plan passed without a word."""
        planner.CONFIG_PATH = self.config(min_reward_risk=None)
        plan = planner.make("SBIN", 961.85, stop_rule="pct:1", target_rule="0.5R",
                            at=clock.now(AT))
        self.assertTrue(any("min_reward_risk 1" in w for w in plan["warnings"]), plan["warnings"])

    def test_a_config_value_that_is_not_a_number_is_refused_by_name(self):
        """REGRESSION. Both crashed with a ValueError traceback."""
        for key in ("min_reward_risk", "min_stop_percent"):
            said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop", "pct:1",
                                "--config", self.config(**{key: "abc"}), "--now", AT)
            self.assertIn(key, said)


@unittest.skipUnless(has(ENTRY) and has(PLAN), "run capture.py")
class ADamagedFileIsARefusal(_Files):
    """REGRESSION. Every one of these printed a Python traceback."""

    def test_show_on_damaged_plans(self):
        text = json.dumps(fixture(PLAN))
        bodies = {"truncated": text[:40], "a list": json.dumps([fixture(PLAN)]),
                  "entry as text": dict(fixture(PLAN), entry="961.85"),
                  "entry of 0": dict(fixture(PLAN), entry=0)}
        for name, body in bodies.items():
            with self.subTest(name):
                self.refused("show", "--plan", self.write("plan.json", body), "--now", AT)

    def test_plan_on_damaged_triggers(self):
        window = dict(fixture(ENTRY)["opening_range"])
        del window["low"]
        bodies = {"no range low": dict(fixture(ENTRY), opening_range=window),
                  "price as words": dict(fixture(ENTRY), entry="abc"),
                  "a list": [fixture(ENTRY)],
                  "truncated": json.dumps(fixture(ENTRY))[:30]}
        for name, body in bodies.items():
            with self.subTest(name):
                self.refused("plan", "--from", self.write("entry.json", body), "--now", AT)

    def test_a_missing_from_file_says_so_plainly(self):
        for extra in ((), ("--entry", "961.85")):
            said = self.refused("plan", "--from", str(Path(self.tmp.name) / "none.json"),
                                "--now", AT, *extra)
            self.assertIn("no trigger", said)
            self.assertNotIn("Errno", said)


@unittest.skipUnless(has(ENTRY), "run capture.py")
class TheDateIsChecked(_Files):
    def test_a_saved_date_that_is_not_yyyy_mm_dd_is_refused(self):
        """REGRESSION. '22/10/2026' from the trigger file was written into the plan."""
        for bad in ("22/10/2026", "20261022"):
            said = self.refused("plan", "--from", self.write("entry.json",
                                dict(fixture(ENTRY), date=bad)), "--now", AT)
            self.assertIn("YYYY-MM-DD", said)


@unittest.skipUnless(has(ENTRY), "run capture.py")
class TheWordsAreTrue(_Files):
    def setUp(self):
        super().setUp()
        self.window = fixture(ENTRY)["opening_range"]

    def make(self, **kw):
        return planner.make("SBIN", 961.85, at=clock.now(AT), **kw)

    def test_rounded_is_said_only_when_rounding_happened(self):
        mid = self.make(window=self.window)
        self.assertIn("= Rs 954.675, rounded down to the tick Rs 954.65", mid["stop_why"])
        low = self.make(stop_rule="or-low", window=self.window)
        self.assertNotIn("rounded", low["stop_why"])
        self.assertNotIn("rounded", self.make(stop_rule="price:955")["stop_why"])
        named = self.make(stop_rule="price:955.03")
        self.assertEqual(named["stop"], 955.00)
        self.assertIn("your price Rs 955.03, rounded down to the tick Rs 955.00", named["stop_why"])

    def test_a_percent_stop_shows_its_working(self):
        plan = self.make(stop_rule="pct:1")
        self.assertIn("Rs 961.85 x (1 - 1%) = Rs 952.2315, rounded down to the tick Rs 952.20",
                      plan["stop_why"])

    def test_a_price_target_shows_the_r_it_gives(self):
        plan = self.make(window=self.window, target_rule="price:1000.02")
        self.assertIn("your price Rs 1,000.02, rounded up to the tick Rs 1,000.05", plan["target_why"])
        self.assertIn(f"({plan['reward_risk']:g}R)", plan["target_why"])

    def test_an_r_target_shows_its_working(self):
        plan = self.make(window=self.window)
        self.assertIn("2R (two times the risk): Rs 961.85 + 2 x Rs 7.20 = Rs 976.25",
                      plan["target_why"])
        self.assertNotIn("rounded", plan["target_why"])

    def test_show_works_out_not_today_again_rather_than_reprinting_it(self):
        """REGRESSION. A plan made on the 23rd for the 22nd reprinted '...not today (2026-10-23)'."""
        stale = dict(fixture(PLAN), warnings=["this plan is for 2026-10-22, not today (2026-10-23)"])
        where = self.write("plan.json", stale)
        code, said = cli("show", "--plan", where, "--now", "2026-10-22 11:00")
        self.assertEqual(code, 0, said)
        self.assertNotIn("not today", said)
        code, said = cli("show", "--plan", where, "--now", "2026-10-24 09:00")
        self.assertIn("not today (2026-10-24)", said)
        self.assertNotIn("2026-10-23", said)


@unittest.skipUnless(has(ENTRY), "run capture.py")
class WhatTheReferenceDoes(_Files):
    def setUp(self):
        super().setUp()
        self.window = fixture(ENTRY)["opening_range"]

    def test_a_bare_r_is_not_a_target(self):
        """REGRESSION. 'R' was read as 1R; the US pack refuses it."""
        with self.assertRaises(planner.Refused):
            planner.make("SBIN", 961.85, window=self.window, target_rule="R", at=clock.now(AT))

    def test_the_us_aliases_for_the_range_stops(self):
        for alias, rule, stop in (("mid", "or-mid", 954.65), ("or-midpoint", "or-mid", 954.65),
                                  ("low", "or-low", 950.05), ("OR_LOW", "or-low", 950.05)):
            plan = planner.make("SBIN", 961.85, stop_rule=alias, window=self.window,
                                at=clock.now(AT))
            self.assertEqual((plan["stop_rule"], plan["stop"]), (rule, stop))

    def test_an_atr_stop_records_its_atr(self):
        rows = bars.candles("SBIN", "5m", day="2026-10-22", offline=SBIN_BARS)
        plan = planner.make("SBIN", 961.85, stop_rule="atr:1.5", day="2026-10-22",
                            offline=SBIN_BARS, entry_bar=12, at=clock.now(AT))
        self.assertEqual(plan["atr_bars"], 12)
        self.assertEqual(plan["atr"], bars.atr(rows[:12])[0])
        self.assertNotIn("atr", planner.make("SBIN", 961.85, window=self.window, at=clock.now(AT)))

    def test_target_r_is_the_multiple_asked_for(self):
        """2.5R on a Rs 2.45 risk is Rs 967.975, which the tick takes up to 968.00: 2.51R."""
        plan = planner.make("SBIN", 961.85, stop_rule="pct:0.25", target_rule="2.5R",
                            at=clock.now(AT))
        self.assertEqual((plan["risk_per_share"], plan["target"]), (2.45, 968.00))
        self.assertEqual(plan["target_r"], 2.5)
        self.assertEqual(plan["target_r_actual"], 2.51)
        self.assertEqual(plan["reward_risk"], 2.51)
        priced = planner.make("SBIN", 961.85, stop_rule="pct:1", target_rule="price:980",
                              at=clock.now(AT))
        self.assertEqual(priced["target_r"], priced["target_r_actual"])

    def test_an_entry_off_the_tick_gets_a_note(self):
        plan = planner.make("SBIN", 961.83, stop_rule="pct:1", at=clock.now(AT))
        self.assertTrue(any("not on the Rs 0.05 tick" in w for w in plan["warnings"]))
        clean = planner.make("SBIN", 961.85, stop_rule="pct:1", at=clock.now(AT))
        self.assertFalse(any("tick NSE accepts" in w for w in clean["warnings"]))


def _sbin_chart():
    return json.loads(Path(SBIN_BARS).read_text(encoding="utf-8"))


def _slot(chart, hhmm, day="2026-10-22"):
    """The index of the real bar that starts at hhmm IST on the day."""
    from datetime import datetime
    stamps = chart["chart"]["result"][0]["timestamp"]
    return next(i for i, s in enumerate(stamps)
                if datetime.fromtimestamp(s, clock.IST).strftime("%Y-%m-%d %H:%M") == f"{day} {hhmm}")


@unittest.skipUnless(has(ENTRY), "run capture.py")
class TheMoneyIsExact(_Files):
    """REGRESSION. Binary floats rounded figures the US pack's Decimal gets right."""

    def test_reward_risk_rounds_half_up_exactly(self):
        """Risk 10.80, reward 14.85: exactly 1.375, so 1.38. The float 14.85/10.8 gave 1.37."""
        plan = planner.make("SBIN", 961.85, stop_rule="price:951.05", target_rule="price:976.70",
                            at=clock.now(AT))
        self.assertEqual((plan["risk_per_share"], plan["reward_per_share"]), (10.80, 14.85))
        self.assertEqual(plan["reward_risk"], 1.38)

    def test_a_range_rounds_half_up_to_the_paisa(self):
        """950.055 half up is 950.06; Python's round() on the float gave 950.05."""
        plan = planner.make("SBIN", 961.85, stop_rule="or-low", at=clock.now(AT),
                            window={"high": 959.30, "low": 950.055})
        self.assertEqual(plan["opening_range"]["low"], 950.06)
        self.assertEqual(plan["stop"], 950.05)          # 950.06 is off the 0.05 tick: down to 950.05

    def test_an_entry_rounded_to_the_paisa_says_so(self):
        plan = planner.make("SBIN", 961.845, stop_rule="pct:1", at=clock.now(AT))
        self.assertEqual(plan["entry"], 961.85)
        self.assertTrue(any("Rs 961.845 was rounded half up" in w for w in plan["warnings"]))
        clean = planner.make("SBIN", 961.85, stop_rule="pct:1", at=clock.now(AT))
        self.assertFalse(any("rounded half up" in w for w in clean["warnings"]))

    def test_the_working_is_exact_never_rounded_for_show(self):
        plan = planner.make("SBIN", 961.85, stop_rule="pct:0.3333", at=clock.now(AT))
        self.assertIn("= Rs 958.64415395, rounded down to the tick Rs 958.60", plan["stop_why"])

    def test_the_atr_stop_uses_six_places_as_the_us_pack_does(self):
        rows = bars.candles("SBIN", "5m", day="2026-10-22", offline=SBIN_BARS)
        exact = bars.atr(rows[:12], places=None)[0]
        plan = planner.make("SBIN", 961.85, stop_rule="atr:1.5", day="2026-10-22",
                            offline=SBIN_BARS, entry_bar=12, at=clock.now(AT))
        self.assertIn(f"1.5 x {round(exact, 6)} = Rs 957.0562505", plan["stop_why"])
        self.assertEqual(plan["atr"], round(exact, 4))
        self.assertEqual(plan["stop_rule"], "atr:1.5")

    def test_the_risk_floor_is_shown_as_it_is(self):
        """0.05% of 961.85 is 0.480925: printed as 0.48 it read 'under Rs 0.48' for a 0.48 risk."""
        with self.assertRaises(planner.Refused) as caught:
            planner.make("SBIN", 961.85, stop_rule="price:961.40", at=clock.now(AT))
        self.assertIn("under Rs 0.480925", str(caught.exception))

    def test_a_stop_just_under_the_gate_never_reads_as_on_it(self):
        """9.60 on 961.85 is 0.998%: to two places 1.00%, which read as passing a 1% gate."""
        plan = planner.make("SBIN", 961.85, stop_rule="price:952.25", at=clock.now(AT))
        self.assertIn("0.9980% from the entry", plan["warning"])
        self.assertIn("the default min_stop_percent", plan["warning"])

    def test_a_number_a_person_does_not_write_is_refused(self):
        for bad in ("price:1e3", "price:nan", "pct:inf"):
            with self.assertRaises(planner.Refused):
                planner.make("SBIN", 961.85, stop_rule=bad, at=clock.now(AT))
        plan = planner.make("SBIN", 961.85, stop_rule="price:Rs 955", at=clock.now(AT))
        self.assertEqual((plan["stop"], plan["stop_rule"]), (955.00, "price:955"))


@unittest.skipUnless(has(ENTRY), "run capture.py")
class TheEntryBarIsOnTheClock(_Files):
    def test_a_bar_missing_from_the_feed_lets_nothing_after_the_entry_in(self):
        """REGRESSION. The real SBIN file with its 09:40 bar blanked: the twelfth bar in the list
        was then the 10:15 bar, which closed after the 10:15 entry."""
        chart = _sbin_chart()
        quote = chart["chart"]["result"][0]["indicators"]["quote"][0]
        quote["close"][_slot(chart, "09:40")] = None
        gap = self.write("SBIN-5m.json", chart)
        cut = planner.bars_to_entry("SBIN", "5m", "2026-10-22", gap, 12)
        self.assertEqual(len(cut), 11)
        self.assertEqual(cut[-1]["end"].strftime("%H:%M"), "10:15")

    def test_a_bar_sent_twice_is_counted_once(self):
        chart = _sbin_chart()
        result = chart["chart"]["result"][0]
        at = _slot(chart, "09:40")
        result["timestamp"].insert(at, result["timestamp"][at])
        for key, values in result["indicators"]["quote"][0].items():
            values.insert(at, values[at])
        twice = self.write("SBIN-5m.json", chart)
        self.assertEqual(len(planner.bars_to_entry("SBIN", "5m", "2026-10-22", twice, 12)), 12)

    def test_a_bar_and_a_time_that_disagree_are_refused(self):
        with self.assertRaises(planner.Refused) as caught:
            planner.make("SBIN", 961.85, stop_rule="pct:1", entry_bar=12, entry_time="10:20",
                         at=clock.now(AT))
        self.assertIn("closes at 10:15", str(caught.exception))

    def test_an_entry_before_the_open_is_refused(self):
        with self.assertRaises(planner.Refused) as caught:
            planner.make("SBIN", 961.85, stop_rule="pct:1", entry_time="09:00", at=clock.now(AT))
        self.assertIn("before the 09:15 open", str(caught.exception))

    def test_a_twelve_hour_time_is_read(self):
        plan = planner.make("SBIN", 961.85, stop_rule="pct:1", entry_time="10:15 AM",
                            at=clock.now(AT))
        self.assertEqual(plan["entry_time"], "10:15")
        self.assertIn("at or after the flat-by",
                      self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop",
                                   "pct:1", "--time", "3:25 PM", "--now", AT))

    def test_the_range_keeps_the_length_the_trigger_saved(self):
        """A trigger whose range ran 09:15 to 09:45 is a 30-minute range, not the default 15."""
        window = dict(fixture(ENTRY)["opening_range"], to="09:45")
        where = self.write("entry.json", dict(fixture(ENTRY), opening_range=window))
        code, said = cli("plan", "--from", where, "--now", AT, "--json")
        self.assertEqual(code, 0, said)
        self.assertEqual(json.loads(said)["opening_range"]["minutes"], 30)


@unittest.skipUnless(has(ENTRY) and has(PLAN), "run capture.py")
class TheCommandLine(_Files):
    def test_json_with_write_is_still_json(self):
        """REGRESSION. 'wrote ...' was printed after the JSON, so the output did not parse."""
        target = str(Path(self.tmp.name) / "plan.json")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = planner.main(["plan", "--from", self.write("entry.json", fixture(ENTRY)),
                                 "--json", "--write", target, "--now", AT])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["stop"], 954.65)
        self.assertIn("wrote", err.getvalue())
        self.assertEqual(json.loads(Path(target).read_text(encoding="utf-8"))["stop"], 954.65)

    def test_the_report_reads_as_the_us_pack_s(self):
        code, said = cli("plan", "--from", self.write("entry.json", fixture(ENTRY)), "--now", AT)
        self.assertEqual(code, 0, said)
        self.assertIn("Exit plan: SBIN long, Thu 2026-10-22, opening-range breakout on 5-minute "
                      "bars", said)
        self.assertIn("at 10:15 (bar 12)", said)
        self.assertIn("(0.75% of the entry)", said)
        code, said = cli("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop", "pct:1",
                         "--now", AT)
        self.assertIn("close above a level on 5-minute bars", said)

    def test_now_takes_the_us_pack_s_shapes(self):
        for now in ("2026-10-22T05:30:00Z", "2026-10-22 11:00 IST", "2026-10-22T11:00+05:30"):
            code, said = cli("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop", "pct:1",
                             "--now", now, "--json")
            self.assertEqual(code, 0, said)
            self.assertEqual(json.loads(said)["created_at"], "2026-10-22T05:30:00+00:00")
        self.assertIn("--now wants", self.refused("plan", "--symbol", "SBIN", "--entry", "961.85",
                                                  "--now", "yesterday"))

    def test_no_subcommand_prints_the_help(self):
        code, said = cli()
        self.assertEqual(code, 2)
        self.assertIn("plan", said)
        self.assertIn("show", said)

    def test_a_damaged_bars_file_is_a_refusal(self):
        for body in ("{not json", "[1, 2]"):
            said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop",
                                "atr:1.5", "--date", "2026-10-22", "--offline",
                                self.write("bars.json", body), "--now", AT)
            self.assertIn("bars.json", said)          # one line naming the file, whoever words it
            self.assertEqual(len(said.strip().splitlines()), 1, said)
        said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop", "atr:1.5",
                            "--date", "2026-10-22", "--offline",
                            str(Path(self.tmp.name) / "none.json"), "--now", AT)
        self.assertIn("none.json", said)
        self.assertNotIn("Errno", said)

    def test_show_refuses_a_plan_that_contradicts_itself(self):
        for name, changed in {"stop above": {"stop": 970.0}, "target below": {"target": 950.0},
                              "risk not entry less stop": {"risk_per_share": 5.0},
                              "setup not text": {"setup": ["orb"]},
                              "flat_by not text": {"flat_by": 1520}}.items():
            with self.subTest(name):
                self.refused("show", "--plan", self.write("plan.json", dict(fixture(PLAN), **changed)),
                             "--now", AT)

    def test_a_config_value_says_your_and_a_default_says_the_default(self):
        planner.CONFIG_PATH = self.config(min_stop_percent=2)
        plan = planner.make("SBIN", 961.85, stop_rule="pct:1", at=clock.now(AT))
        self.assertIn("inside your min_stop_percent of 2%", plan["warning"])
        planner.CONFIG_PATH = self.config(min_stop_percent=None)
        plan = planner.make("SBIN", 961.85, stop_rule="pct:0.5", at=clock.now(AT))
        self.assertIn("inside the default min_stop_percent of 1%", plan["warning"])

    def test_the_plan_keeps_the_working_apart_as_the_us_pack_does(self):
        plan = planner.make("SBIN", 961.85, window=fixture(ENTRY)["opening_range"],
                            at=clock.now(AT))
        self.assertEqual(plan["stop_detail"],
                         "(Rs 959.30 + Rs 950.05) / 2 = Rs 954.675, rounded down to the tick "
                         "Rs 954.65")


def _ist(stamp_text):
    from datetime import datetime
    return int(datetime.strptime(stamp_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=clock.IST)
               .timestamp())


@unittest.skipUnless(has(ENTRY) and has(PLAN), "run capture.py")
class HostileInputsAreRefusals(_Files):
    """REGRESSION. Each of these was a traceback, or a plan carrying garbage, before."""

    def test_a_number_too_large_to_be_a_price(self):
        said = self.refused("plan", "--from", self.write("entry.json",
                            json.dumps(dict(fixture(ENTRY), entry=1e308))), "--now", AT)
        self.assertIn("too large", said)
        self.assertIn("too large", self.refused("plan", "--symbol", "SBIN", "--entry", "1e300",
                                                "--stop", "pct:1", "--now", AT))
        self.assertIn("too large", self.refused("plan", "--symbol", "SBIN", "--entry", "961.85",
                                                "--stop", "pct:1", "--target",
                                                "99999999999999999999999999999R", "--now", AT))

    def test_a_range_length_that_is_not_a_length(self):
        for bad in ("NaN", "1e308"):
            body = json.dumps(fixture(ENTRY)).replace('"bars": 3', f'"bars": 3, "minutes": {bad}')
            said = self.refused("plan", "--from", self.write("entry.json", body), "--now", AT)
            self.assertIn("minutes", said)

    def test_an_upside_down_range_is_refused_whatever_the_stop(self):
        said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--or-high", "950.05",
                            "--or-low", "959.30", "--stop", "pct:1", "--now", AT)
        self.assertIn("must be above its low", said)

    def test_a_cut_off_or_corrupt_gzip_chart(self):
        import gzip
        whole = gzip.compress(Path(SBIN_BARS).read_bytes())
        broken = bytearray(whole)
        broken[len(broken) // 2] ^= 0xFF
        for name, body in (("cut", whole[:300]), ("corrupt", bytes(broken))):
            where = Path(self.tmp.name) / f"SBIN-{name}.json.gz"
            where.write_bytes(body)
            said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop",
                                "atr:1.5", "--date", "2026-10-22", "--offline", str(where),
                                "--now", AT)
            self.assertIn(where.name, said)

    def test_a_last_trade_time_no_clock_has(self):
        chart = _sbin_chart()
        chart["chart"]["result"][0]["meta"]["regularMarketTime"] = 1e308
        said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop", "atr:1.5",
                            "--date", "2026-10-22", "--offline",
                            self.write("SBIN-5m.json", chart), "--now", AT)
        self.assertIn("SBIN-5m.json", said)

    def test_a_now_outside_the_calendar(self):
        said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop", "pct:1",
                            "--now", "0001-01-01 00:00")
        self.assertIn("--now", said)

    def test_a_config_that_is_a_folder_says_so(self):
        said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop", "pct:1",
                            "--config", self.tmp.name, "--now", AT)
        self.assertIn("is a folder", said)
        self.assertNotIn("Errno", said)

    def test_writing_to_a_folder_writes_nothing_and_says_so(self):
        said = self.refused("plan", "--from", self.write("entry.json", fixture(ENTRY)),
                            "--write", self.tmp.name, "--now", AT)
        self.assertIn("is a folder", said)
        self.assertNotIn("Exit plan", said)
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), ["entry.json"])

    def test_a_file_named_nse_in_the_way_is_a_refusal(self):
        """REGRESSION. The default nse/entry.json and nse/plan.json made the folder first, and
        a file called nse in the working folder made that a FileExistsError traceback."""
        import os
        (Path(self.tmp.name) / "nse").write_text("not a folder", encoding="utf-8")
        here = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, here)
        self.assertIn("no trigger", self.refused("plan", "--now", AT))
        code, said = cli("show", "--now", AT)
        self.assertEqual(code, 2, said)
        self.assertNotIn("Traceback", said)
        said = self.refused("plan", "--symbol", "SBIN", "--entry", "961.85", "--stop", "pct:1",
                            "--write", "--now", AT)
        self.assertIn("could not be written", said)

    def test_a_trigger_field_that_is_not_text(self):
        """A symbol of true was planned as TRUE; a trigger_time of 0 was saved as the entry time."""
        for key, bad in (("symbol", True), ("trigger_time", 0), ("trigger_time", False),
                         ("date", 20261022)):
            with self.subTest(key=key, bad=bad):
                said = self.refused("plan", "--from", self.write("entry.json",
                                    dict(fixture(ENTRY), **{key: bad})), "--now", AT)
                self.assertIn(key, said)


@unittest.skipUnless(has(ENTRY), "run capture.py")
class NothingTheFeedHadNotFinished(_Files):
    def test_a_bar_ending_after_the_feeds_last_trade_is_not_counted(self):
        """The real SBIN chart, saved as if at 11:02 on 2026-10-22: the 11:00 bar was two minutes
        old then. The US pack drops it by the chart's regularMarketTime; so does this."""
        chart = _sbin_chart()
        chart["chart"]["result"][0]["meta"]["regularMarketTime"] = _ist("2026-10-22 11:02:00")
        saved = self.write("SBIN-5m.json", chart)
        rows = planner.bars_to_entry("SBIN", "5m", "2026-10-22", saved,
                                     at=clock.now("2026-10-22 15:00"))
        self.assertEqual(rows[-1]["end"].strftime("%H:%M"), "11:00")
        whole = planner.bars_to_entry("SBIN", "5m", "2026-10-22", SBIN_BARS,
                                      at=clock.now("2026-10-22 15:00"))
        self.assertEqual(whole[-1]["end"].strftime("%H:%M"), "15:00")

    def test_an_entry_bar_at_or_after_flat_by_is_refused(self):
        """Bar 73 of a 5-minute day closes 15:20, the flat-by time: no trade is left in it."""
        with self.assertRaises(planner.Refused) as caught:
            planner.make("SBIN", 961.85, stop_rule="pct:1", entry_bar=73, at=clock.now(AT))
        self.assertIn("closes at 15:20", str(caught.exception))
        self.assertEqual(planner.make("SBIN", 961.85, stop_rule="pct:1", entry_bar=72,
                                      at=clock.now(AT))["entry_bar"], 72)


@unittest.skipUnless(has(ENTRY), "run capture.py")
class TheReferenceKeysAreRead(_Files):
    def test_price_and_time_stand_in_for_entry_and_trigger_time(self):
        """The US pack reads a trigger's price and time; /nse-entry writes both pairs."""
        saved = dict(fixture(ENTRY), price=fixture(ENTRY)["entry"], time="10:15")
        del saved["entry"], saved["trigger_time"]
        code, said = cli("plan", "--from", self.write("entry.json", saved), "--now", AT, "--json")
        self.assertEqual(code, 0, said)
        plan = json.loads(said)
        self.assertEqual((plan["entry"], plan["entry_time"], plan["stop"]), (961.85, "10:15", 954.65))

    def test_the_target_rule_is_stored_as_the_number(self):
        plan = planner.make("SBIN", 961.85, stop_rule="pct:1", target_rule="price:Rs 1,000",
                            at=clock.now(AT))
        self.assertEqual(plan["target_rule"], "price:1000")
        self.assertEqual(planner.make("SBIN", 961.85, stop_rule="pct:1", target_rule="2.50r",
                                      at=clock.now(AT))["target_rule"], "2.5R")


@unittest.skipUnless(has(ENTRY), "run capture.py")
class TheGateWarningIsTrue(_Files):
    def test_the_gate_is_nse_risks_own_exact_rule(self):
        """SBIN really closed at 965.00 at 12:25 on 2026-10-22. pct:1 is 955.35, exactly 1%:
        /nse-risk compares in whole paise and passes it, so no warning. 955.40 is a paisa
        inside and /nse-risk blocks it, so it is warned, and the gate decides both the same."""
        import importlib.util
        rows = bars.candles("SBIN", "5m", day="2026-10-22", offline=SBIN_BARS)
        price = next(b["close"] for b in rows if b["start"].strftime("%H:%M") == "12:25")
        self.assertEqual(price, 965.00)
        on = planner.make("SBIN", price, stop_rule="pct:1", at=clock.now(AT))
        self.assertEqual((on["stop"], on["risk_percent"]), (955.35, 1.0))
        self.assertIsNone(on["warning"])
        inside = planner.make("SBIN", price, stop_rule="price:955.40", at=clock.now(AT))
        self.assertIn("inside the default min_stop_percent of 1%", inside["warning"])
        self.assertIn("--stop pct:1)", inside["warning"])
        spec = importlib.util.spec_from_file_location(
            "risk_gate", Path(__file__).resolve().parent.parent / "nse-risk" / "risk.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        for stop, warned in ((955.35, False), (955.40, True)):
            sized = gate.size(price, stop, dict(gate.DEFAULTS))
            blocked = any("closer than" in b for b in sized["reasons"])
            self.assertEqual(blocked, warned, (stop, sized["reasons"]))

    def test_a_reward_risk_shown_as_one_but_under_it_is_warned(self):
        """9.95 on a 10.00 risk is 0.995, shown 1.0: /nse-budget refuses it on the exact ratio,
        and this used to say nothing because it compared the rounded one."""
        plan = planner.make("SBIN", 961.85, stop_rule="price:951.85", target_rule="price:971.80",
                            at=clock.now(AT))
        self.assertEqual((plan["risk_per_share"], plan["reward_per_share"], plan["reward_risk"]),
                         (10.0, 9.95, 1.0))
        said = [w for w in plan["warnings"] if "min_reward_risk" in w]
        self.assertEqual(len(said), 1, plan["warnings"])
        self.assertIn("reward:risk 0.9950 is under the default min_reward_risk 1", said[0])

    def test_only_a_checked_stop_is_offered(self):
        """'--stop or-low would pass' was said with no range, and of a plan already on or-low."""
        window = fixture(ENTRY)["opening_range"]
        bare = planner.make("SBIN", 961.85, stop_rule="pct:0.5", at=clock.now(AT))
        self.assertNotIn("or-low", bare["warning"])
        self.assertIn("--stop pct:1)", bare["warning"])
        planner.CONFIG_PATH = self.config(min_stop_percent=2)
        low = planner.make("SBIN", 961.85, stop_rule="or-low", window=window, at=clock.now(AT))
        self.assertIn("min_stop_percent of 2%", low["warning"])
        self.assertNotIn("or-low", low["warning"])
        mid = planner.make("SBIN", 961.85, window=window, at=clock.now(AT))
        self.assertNotIn("or-low", mid["warning"])      # 1.23% is under 2% too
        planner.CONFIG_PATH = self.config(min_stop_percent=None)
        mid = planner.make("SBIN", 961.85, window=window, at=clock.now(AT))
        self.assertIn("--stop or-low, or --stop pct:1)", mid["warning"])


@unittest.skipUnless(has(PLAN), "run capture.py")
class ShowPrintsOnlyWhatThePlanSays(_Files):
    def test_a_saved_field_that_would_print_garbage_is_refused(self):
        for key, bad in (("reward_risk", 5.0), ("interval", "abc"), ("entry_time", "abc"),
                         ("entry_bar", 0), ("entry_bar", 13), ("flat_by", "abc"),
                         ("stop_why", True), ("symbol", "")):
            with self.subTest(key=key):
                self.refused("show", "--plan", self.write("plan.json",
                             dict(fixture(PLAN), **{key: bad})), "--now", AT)

    def test_a_null_setup_or_interval_is_the_default_not_none(self):
        code, said = cli("show", "--plan", self.write("plan.json", dict(
            fixture(PLAN), setup=None, interval=None, warning=True)), "--now", AT)
        self.assertEqual(code, 0, said)
        self.assertIn("opening-range breakout on 5-minute bars", said)
        self.assertNotIn("None", said)
        self.assertNotIn("WARNING: True", said)


if __name__ == "__main__":
    unittest.main(verbosity=2)
