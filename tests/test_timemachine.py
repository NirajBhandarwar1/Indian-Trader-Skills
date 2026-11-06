"""The replay: what the odds mean, what ties to, and why break-even is not a win rate.

Runs against saved real SBIN bars, so the pool is the market's own, not invented.
"""
import random
from datetime import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import FIXTURES, has, path, skill                     # noqa: E402
from nsetools import bars                                        # noqa: E402

machine = skill("nse-timemachine", "timemachine")
BARS = FIXTURES / "bars" / "SBIN-5m.json"
POOL = FIXTURES / "bars" / "SBIN-60d5m.json.gz"     # sixty real sessions, the replay's own pool

PLAN = {"symbol": "SBIN", "entry": 961.85, "stop": 950.00, "target": 985.55,
        "date": "2026-10-22", "entry_time": "10:15", "flat_by": "15:20", "interval": "5m",
        "setup": "orb"}


class BreakEvenIsNotAWinRate(unittest.TestCase):
    """The thing this skill exists to say correctly."""

    def test_a_bigger_target_needs_to_win_less_often(self):
        for reward, expected in ((1.0, 50.0), (2.0, 33.3), (3.0, 25.0)):
            self.assertAlmostEqual(100 / (1 + reward), expected, places=1)

    def test_a_two_r_plan_winning_a_third_of_the_time_is_level_not_losing(self):
        """32% at 2R is close to break-even. Reading it as 'fails two in three' is the mistake."""
        said = machine.verdict({"target": 32, "stop": 68}, 100, 2.0)
        self.assertIn("break-even", said)
        self.assertNotIn("Strongly", said)

    def test_a_clear_lean_to_the_stop_is_named(self):
        said = machine.verdict({"target": 5, "stop": 95}, 100, 2.0)
        self.assertIn("stop", said)
        self.assertIn("Strongly", said)

    def test_a_clear_lean_to_the_target_is_named(self):
        said = machine.verdict({"target": 70, "stop": 30}, 100, 2.0)
        self.assertIn("target", said)
        self.assertIn("Strongly", said)

    def test_a_one_r_plan_near_half_is_called_a_coin_flip(self):
        said = machine.verdict({"target": 50, "stop": 50}, 100, 1.0)
        self.assertIn("coin flip", said)

    def test_nothing_decided_says_so_rather_than_dividing_by_zero(self):
        said = machine.verdict({"target": 0, "stop": 0}, 0, 2.0, "13:00")
        self.assertTrue(said.startswith("No replay reached the target or the stop by 13:00"))
        self.assertIn("nothing was decided", said)


@unittest.skipUnless(BARS.exists(), "fixture not saved")
class TheStopWinsATie(unittest.TestCase):
    """A bar that touches both counts as the stop, every time.

    The bars below are real SBIN five-minute bars, turned into ratios by the real code. Only the
    stop and target levels are chosen, so that a real bar's own range spans both of them.
    """

    @classmethod
    def setUpClass(cls):
        rows = bars.candles("SBIN", "5m", day="2026-10-22", offline=str(BARS))
        cls.steps = machine.ratios(rows)
        # A bar that STRADDLES the previous close, so one real bar can reach a stop below and a
        # target above. Not every bar does: the widest one that session gapped up, and its low
        # never went under the close before it.
        straddling = [s for s in cls.steps if s["low"] < 1.0 < s["high"]]
        if not straddling:
            raise unittest.SkipTest("no bar straddled its previous close that session")
        cls.widest = max(straddling, key=lambda s: s["high"] - s["low"])

    def pool(self, step):
        return {step["at"]: [step]}

    def test_a_real_bar_that_spans_both_levels_counts_as_the_stop(self):
        """Levels set just inside the widest real bar's own high and low.

        From a bar nobody can tell which side came first, so the pessimistic order is the honest
        one: a replay that takes the target when both were touched invents money.
        """
        step = self.widest
        start = 100.0
        # The bar's own high and low. The comparisons are inclusive, so this one real bar reaches
        # both levels exactly, which is the tie.
        stop, target = start * step["low"], start * step["high"]
        self.assertLess(stop, start)
        self.assertGreater(target, start)
        how, r = machine.one_replay(start, stop, target, [step["at"]], self.pool(step),
                                    random.Random(1))
        self.assertEqual(how, "stop")
        self.assertEqual(r, -1.0)

    def test_a_real_up_bar_that_only_reaches_the_target_counts_as_the_target(self):
        step = max(self.steps, key=lambda s: s["high"])
        start = 100.0
        target = start * step["high"] * 0.999
        stop = start * (step["low"] - 0.05)
        how, _ = machine.one_replay(start, stop, target, [step["at"]], self.pool(step),
                                    random.Random(1))
        self.assertEqual(how, "target")

    def test_a_real_bar_reaching_neither_leaves_the_trade_open(self):
        step = min(self.steps, key=lambda s: s["high"] - s["low"])
        start = 100.0
        how, _ = machine.one_replay(start, start * 0.90, start * 1.10, [step["at"]],
                                    self.pool(step), random.Random(1))
        self.assertEqual(how, "neither")

    def test_an_r_of_one_comes_back_from_a_one_r_target(self):
        step = max(self.steps, key=lambda s: s["high"])
        start = 100.0
        target = start * step["high"] * 0.999
        stop = start - (target - start)
        _, r = machine.one_replay(start, stop, target, [step["at"]], self.pool(step),
                                  random.Random(1))
        self.assertAlmostEqual(r, 1.0, places=6)

    def test_the_ratios_come_from_the_real_bars(self):
        """Each step is a real bar divided by the close before it, so it rescales to any price."""
        self.assertGreater(len(self.steps), 60)
        for step in self.steps:
            self.assertGreater(step["high"], 0)
            self.assertGreaterEqual(step["high"], step["low"])


@unittest.skipUnless(BARS.exists(), "fixture not saved")
class OnRealBars(unittest.TestCase):
    def test_the_session_length_is_measured_not_assumed(self):
        """NSE's 09:15-15:30 is 75 bars on paper, and the feed often sends fewer: in the saved
        sixty days, 57 of SBIN's sessions came back with the 15:20 bar empty.

        A hard-coded 75 rejected every session there was and the skill answered "no history".
        """
        found = machine.sessions("SBIN", "2026-10-22", offline=str(BARS))
        self.assertGreaterEqual(len(found), 1)
        self.assertLess(machine.FULL_ENOUGH, 1.0)

    def test_only_sessions_before_the_plans_date_are_used(self):
        for day in machine.sessions("SBIN", "2026-10-22", offline=str(BARS)):
            self.assertLess(day[0]["start"].date().isoformat(), "2026-10-22")

    def test_the_pool_is_grouped_by_time_of_day(self):
        """The market at 09:20 is not the market at 14:50."""
        found = machine.sessions("SBIN", "2026-10-22", offline=str(BARS))
        by_time = machine.pool(found, least=1)      # five saved days: under MIN_SESSIONS each
        self.assertIn("09:20", by_time)
        self.assertIn("14:50", by_time)

    def test_the_same_seed_gives_the_same_answer(self):
        one = machine.replay(PLAN, runs=300, seed=7, offline=str(POOL))
        two = machine.replay(PLAN, runs=300, seed=7, offline=str(POOL))
        self.assertEqual(one["target_first_percent"], two["target_first_percent"])

    def test_the_three_outcomes_add_up_to_everything(self):
        found = machine.replay(PLAN, runs=500, seed=3, offline=str(POOL))
        total = (found["target_first_percent"] + found["stop_first_percent"]
                 + found["neither_percent"])
        self.assertAlmostEqual(total, 100.0, places=1)

    def test_the_history_is_counted_separately_from_the_replays(self):
        """The replays are a simulation. The history is what the days did."""
        found = machine.replay(PLAN, runs=200, seed=3, offline=str(POOL))
        past = found["history"]
        self.assertEqual(past["target"] + past["stop"] + past["close"], past["triggered"])
        self.assertEqual(len(past["trades"]), past["triggered"])

    def test_a_plan_with_no_history_refuses_rather_than_guessing(self):
        with self.assertRaises(machine.NotEnough):
            machine.replay(dict(PLAN, date="2020-01-01"), runs=10, offline=str(POOL))


@unittest.skipUnless(POOL.exists(), "fixture not saved")
class APlanGivenAsFlags(unittest.TestCase):
    """--symbol --entry --stop --target, so a replay does not need /nse-exit to have run first."""

    def args(self, **over):
        import argparse
        base = dict(symbol="SBIN", entry=961.85, stop=950.0, target="2R", date="2026-10-22",
                    entry_time="10:15", flat_by=None, setup=None, side="buy", or_minutes=None,
                    stop_rule="auto")
        base.update(over)
        return argparse.Namespace(**base)

    def test_a_multiple_of_the_risk_becomes_a_price(self):
        plan = machine.inline_plan(self.args(target="2R"))
        self.assertEqual(plan["target"], round(961.85 + 2 * 11.85, 2))

    def test_a_price_is_taken_as_given(self):
        self.assertEqual(machine.inline_plan(self.args(target="980"))["target"], 980.0)

    def test_it_gives_the_same_answer_as_the_written_plan(self):
        """The same numbers by flags or by file must replay the same way."""
        written = dict(PLAN)
        flagged = machine.inline_plan(self.args(target=str(PLAN["target"])))
        one = machine.replay(written, runs=400, seed=5, offline=str(POOL))
        two = machine.replay(flagged, runs=400, seed=5, offline=str(POOL))
        self.assertEqual(one["target_first_percent"], two["target_first_percent"])
        self.assertEqual(one["stop_first_percent"], two["stop_first_percent"])

    def test_a_stop_above_the_entry_is_refused(self):
        with self.assertRaises(machine.NotEnough):
            machine.inline_plan(self.args(stop=970.0))

    def test_a_target_below_the_entry_is_refused(self):
        with self.assertRaises(machine.NotEnough):
            machine.inline_plan(self.args(target="900"))


@unittest.skipUnless(POOL.exists(), "fixture not saved")
class TheTimeOfDayWindow(unittest.TestCase):
    """--window: draw past bars from the same time of day, plus or minus this many bars."""

    def test_the_window_is_recorded_with_the_result(self):
        found = machine.replay(PLAN, runs=200, seed=3, offline=str(POOL), spread=4)
        self.assertEqual(found["window"], 4)

    def test_a_window_of_nothing_draws_only_the_exact_time(self):
        """With no spread the pool for each step is just that minute's past bars, so the answer
        changes -- which is the point of letting the user see it."""
        narrow = machine.replay(PLAN, runs=1500, seed=3, offline=str(POOL), spread=0)
        wide = machine.replay(PLAN, runs=1500, seed=3, offline=str(POOL), spread=4)
        self.assertNotEqual(narrow["neither_percent"], wide["neither_percent"])


@unittest.skipUnless(POOL.exists(), "run capture.py")
class SixtySessionsAndTheirOwnRanges(unittest.TestCase):
    """The pool is the stock's last 60 sessions, and each past day is judged by its own range."""

    @classmethod
    def setUpClass(cls):
        cls.found = machine.replay(PLAN, runs=500, seed=3, offline=str(POOL))
        cls.days = machine.days_of("SBIN", "5m", str(POOL))

    def test_the_pool_is_the_sixty_day_window_not_five(self):
        self.assertGreaterEqual(self.found["sessions_used"], machine.MIN_SESSIONS)
        self.assertEqual(self.found["pool_from"], "2026-07-29")
        self.assertEqual(self.found["pool_to"], "2026-10-21")

    def test_five_days_of_bars_is_refused_rather_than_resampled(self):
        with self.assertRaises(machine.NotEnough) as said:
            machine.replay(PLAN, runs=10, offline=str(BARS))
        self.assertIn("at least 20", str(said.exception))

    def test_auto_finds_the_stop_was_the_opening_range_low(self):
        """The stop 950.05 is the real 2026-10-22 range low, to the tick."""
        found = machine.replay(dict(PLAN, stop=950.05, target=985.45), runs=50,
                               offline=str(POOL))
        self.assertEqual(found["history"]["rule"]["stop_rule"], "or-low")

    def test_auto_finds_a_midpoint_stop(self):
        window = machine._opening_range(self.days["2026-10-22"], 15)
        mid = machine.clock.to_tick(sum(window) / 2, up=False)
        self.assertEqual(mid, 954.65)
        found = machine.replay(dict(PLAN, stop=mid, target=980.0), runs=50, offline=str(POOL))
        self.assertEqual(found["history"]["rule"]["stop_rule"], "or-mid")

    def test_any_other_stop_is_replayed_as_its_percentage(self):
        found = machine.replay(dict(PLAN, stop=951.0), runs=50, offline=str(POOL))
        rule = found["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "pct")
        self.assertAlmostEqual(rule["stop_pct"], round(100 * (961.85 - 951.0) / 961.85, 4))

    def test_each_past_day_gets_its_own_range_low_as_its_stop(self):
        found = machine.replay(dict(PLAN, stop=950.05, target=985.45), runs=50,
                               offline=str(POOL))
        self.assertGreater(found["history"]["triggered"], 20)
        for trade in found["history"]["trades"]:
            high, low = machine._opening_range(self.days[trade["date"]], 15)
            self.assertEqual(trade["stop"], machine.clock.to_tick(low, up=False), trade)
            self.assertGreater(trade["entry"], high, trade)

    def test_a_trade_r_is_what_the_day_paid(self):
        for trade in self.found["history"]["trades"]:
            risk = trade["entry"] - trade["stop"]
            if trade["outcome"] == "stop":
                self.assertEqual(trade["R"], -1.0)
            elif trade["outcome"] == "target":
                self.assertAlmostEqual(trade["R"], (trade["target"] - trade["entry"]) / risk,
                                       places=2)

    def test_the_first_replayed_bar_is_the_one_after_the_entry(self):
        """Entry at the 10:15 close: the next bar runs 10:15 to 10:20 and the last closes at
        15:20. That is 61 bars. The first version skipped the 10:15 bar and ran to 15:25."""
        self.assertEqual(self.found["bars_left"], 61)

    def test_until_ends_on_the_bar_closing_then(self):
        found = machine.replay(PLAN, runs=50, offline=str(POOL), until="11:15")
        self.assertEqual(found["bars_left"], 12)

    def test_an_entry_the_data_does_not_have_is_refused(self):
        """Pinned to 10:00, the 10:15 bar has not closed, so there is no such entry yet."""
        at = datetime(2026, 10, 22, 10, 0, tzinfo=machine.clock.IST)
        with self.assertRaises(machine.NotEnough) as said:
            machine.replay(PLAN, runs=10, offline=str(POOL), at=at)
        self.assertIn("not in the data", str(said.exception))

    def test_an_entry_outside_its_bar_is_warned_about(self):
        found = machine.replay(dict(PLAN, entry=990.0, stop=980.0, target=1010.0), runs=20,
                               offline=str(POOL))
        self.assertTrue(any("outside that bar" in w for w in found["warnings"]))

    def test_a_setup_that_is_not_orb_skips_the_history_and_says_so(self):
        found = machine.replay(dict(PLAN, setup="level"), runs=20, offline=str(POOL))
        self.assertIsNone(found["history"]["rule"])
        self.assertIn("orb", found["history"]["note"])


class ThePlanIsChecked(unittest.TestCase):
    def test_a_sell_is_refused_with_the_reason(self):
        with self.assertRaises(machine.NotEnough) as said:
            machine.checked(dict(PLAN, side="sell"))
        self.assertIn("buys only", str(said.exception))

    def test_an_entry_off_the_bar_grid_is_refused(self):
        with self.assertRaises(machine.NotEnough):
            machine.checked(dict(PLAN, entry_time="10:17"))

    def test_one_minute_bars_are_refused_with_the_measured_reason(self):
        with self.assertRaises(machine.NotEnough) as said:
            machine.checked(dict(PLAN, interval="1m"))
        self.assertIn("only 8", str(said.exception))

    def test_an_opening_range_must_be_whole_bars(self):
        with self.assertRaises(machine.NotEnough):
            machine.checked(dict(PLAN, or_minutes=12))

    def test_the_plans_opening_range_minutes_are_read(self):
        plan = machine.checked(dict(PLAN, opening_range={"minutes": 30}))
        self.assertEqual(plan["opening_range"]["minutes"], 30)

    def test_a_plan_with_no_entry_time_is_refused(self):
        """As the US pack refuses it: the end of the range is not proof of an entry there."""
        plan = dict(PLAN)
        plan.pop("entry_time")
        with self.assertRaises(machine.NotEnough):
            machine.checked(dict(plan, or_minutes=30))

    def test_a_stop_rule_from_nse_exit_is_understood(self):
        for given, used in (("or-mid", "or-mid"), ("or-low", "or-low"), ("or_low", "or-low"),
                            ("pct:1.2", "pct"), ("price:950", "pct"), ("atr:1.5", "pct")):
            self.assertEqual(machine.checked(dict(PLAN, stop_rule=given))["stop_rule"], used)


def _run(argv):
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = machine.main(argv)
    return code, out.getvalue()


@unittest.skipUnless(POOL.exists(), "fixture not saved")
class AHoleIsNeverBridged(unittest.TestCase):
    """The feed leaves bars empty: SBIN's 15:20 bar on 57 of the 60 saved days. Measured before
    this: the 15:25 bar after the hole went in as a five-minute move when it was ten, and a 15:30
    replay of SBIN's 14:30 plan read target-first 20.6% where the honest pool says 15.4%."""

    @classmethod
    def setUpClass(cls):
        cls.days = machine.days_of("SBIN", "5m", str(POOL))
        cls.history = machine.sessions("SBIN", "2026-10-22", days=cls.days)

    def test_a_pair_across_a_missing_bar_is_left_out(self):
        holed = next(d for d in self.history
                     if "15:20" not in {b["start"].strftime("%H:%M") for b in d}
                     and "15:25" in {b["start"].strftime("%H:%M") for b in d})
        steps = {s["at"] for s in machine.ratios(holed)}
        self.assertNotIn("15:25", steps)              # its bar before is 15:15, ten minutes back
        self.assertIn("15:15", steps)

    def test_every_step_is_one_bar_after_the_one_before(self):
        for day in self.history:
            starts = {b["start"].strftime("%H:%M"): b["start"] for b in day}
            for step in machine.ratios(day):
                at = starts[step["at"]]
                before = (at - machine.STEP).strftime("%H:%M")
                self.assertIn(before, starts, (day[0]["start"].date(), step["at"]))

    def test_a_time_of_day_too_few_sessions_hold_is_left_out_of_the_draw(self):
        sizes = machine.slot_sizes(self.history)
        self.assertLess(sizes["15:20"], machine.MIN_SESSIONS)
        by_time = machine.pool(self.history)
        self.assertNotIn("15:20", by_time)
        self.assertTrue(all(len(v) >= machine.MIN_SESSIONS for v in by_time.values()))

    def test_a_replay_that_needs_a_thin_bar_is_refused_by_name(self):
        with self.assertRaises(machine.NotEnough) as said:
            machine.replay(PLAN, runs=10, offline=str(POOL), until="15:30")
        text = str(said.exception)
        self.assertIn("the 15:20 bar is in only 3 of the 58 past sessions", text)
        self.assertIn("earlier --until", text)

    def test_the_result_says_what_went_in_and_what_was_left_out(self):
        found = machine.replay(PLAN, runs=50, offline=str(POOL))
        self.assertEqual(found["sessions_used"], 58)
        self.assertGreater(found["pairs_skipped_for_holes"], 0)
        self.assertEqual(found["pairs_skipped_for_holes"], machine.holes(self.history))
        self.assertIn("15:20", found["slots_left_out"])
        self.assertLess(found["complete_sessions"], found["sessions_used"])
        said = "\n".join(machine.report(found))
        self.assertIn("left out, never bridged", said)
        self.assertNotIn("full sessions", said)


@unittest.skipUnless(POOL.exists(), "fixture not saved")
class TheGapsTheUSPackClosed(unittest.TestCase):
    def test_a_plan_that_is_not_json_is_refused_in_one_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for body, words in (("{bad", "not a readable plan"), ("[1, 2]", "not a plan")):
                where = Path(tmp) / "plan.json"
                where.write_text(body, encoding="utf-8")
                code, said = _run(["run", "--plan", str(where), "--offline", str(POOL)])
                self.assertEqual(code, 2, said)
                self.assertIn(words, said)

    def test_an_opening_range_of_no_minutes_is_refused_not_made_fifteen(self):
        with self.assertRaises(machine.NotEnough):
            machine.checked(dict(PLAN, opening_range={"minutes": 0}))
        with self.assertRaises(machine.NotEnough):
            machine.checked(dict(PLAN, or_minutes=0))

    def test_an_entry_bar_alone_sets_the_entry_time(self):
        """Bar 12 as /nse-entry counts it is the 10:10 bar, which closes at 10:15."""
        plan = dict(PLAN)
        plan.pop("entry_time")
        self.assertEqual(machine.checked(dict(plan, entry_bar=12))["entry_time"], "10:15")
        one = machine.replay(dict(plan, entry_bar=12), runs=200, seed=3, offline=str(POOL))
        two = machine.replay(PLAN, runs=200, seed=3, offline=str(POOL))
        self.assertEqual(one["target_first_percent"], two["target_first_percent"])

    def test_an_entry_bar_that_disagrees_with_the_entry_time_is_refused(self):
        with self.assertRaises(machine.NotEnough) as said:
            machine.checked(dict(PLAN, entry_bar=11))
        self.assertIn("disagrees with itself", str(said.exception))
        self.assertEqual(machine.checked(dict(PLAN, entry_bar=12))["entry_time"], "10:15")

    def test_the_written_result_keeps_160_sample_paths_and_the_screen_does_not(self):
        import tempfile
        argv = ["run", "--symbol", "SBIN", "--entry", "961.85", "--stop", "950.0", "--target",
                "985.55", "--date", "2026-10-22", "--entry-time", "10:15", "--offline", str(POOL),
                "--replays", "300"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tm.json"
            code, said = _run(argv + ["--out", str(out)])
            self.assertEqual(code, 0, said)
            saved = __import__("json").loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["sample_paths"]), machine.SAMPLE_PATHS)
        self.assertEqual(len(saved["sample_paths"][0]), saved["bars_left"] + 1)
        self.assertEqual(saved["sample_paths"][0][0], 961.85)
        self.assertIn("Next: /nse-budget", said)
        self.assertIn("still open at 15:20", said)
        self.assertIn("still counts -1R", said)
        code, shown = _run(argv + ["--json"])
        self.assertNotIn("sample_paths", __import__("json").loads(shown))

    def test_the_entry_time_help_claims_no_default(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            machine.main(["--help"])
        self.assertNotIn("default the end of the", " ".join(out.getvalue().split()))


def _changed_pool(tmp, change):
    """The real sixty-day SBIN file, copied, with `change(result)` applied to one fact."""
    import gzip
    import json
    with gzip.open(POOL, "rt", encoding="utf-8") as handle:
        doc = json.load(handle)
    change(doc["chart"]["result"][0])
    where = Path(tmp) / "SBIN-60d5m.json"
    where.write_text(json.dumps(doc), encoding="utf-8")
    return str(where)


def _blank(result, day, hhmm):
    """Empty one bar, as the feed leaves bars empty: the stamp stays, the prices go."""
    quote = result["indicators"]["quote"][0]
    for i, stamp in enumerate(result["timestamp"]):
        at = datetime.fromtimestamp(stamp, machine.clock.IST)
        if at.date().isoformat() == day and at.strftime("%H:%M") == hhmm:
            for key in ("open", "high", "low", "close", "volume"):
                quote[key][i] = None
            return
    raise AssertionError(f"no {hhmm} bar on {day}")


@unittest.skipUnless(POOL.exists(), "fixture not saved")
class TheLastSideBySideGaps(unittest.TestCase):
    """Each found by running the US pack's own code beside this one, and closed."""

    @classmethod
    def setUpClass(cls):
        cls.found = machine.replay(dict(PLAN, stop=950.05, target=985.45), runs=300, seed=19,
                                   offline=str(POOL),
                                   at=datetime(2026, 10, 24, 9, 0, tzinfo=machine.clock.IST))

    def test_a_clock_time_past_59_minutes_is_refused_not_rolled_over(self):
        """'10:60' had been read as 11:00 and replayed as if the user had said so."""
        for bad in ("10:60", "24:00", "9:5", "10:15 AM"):
            with self.assertRaises(machine.NotEnough):
                machine.checked(dict(PLAN, entry_time=bad))
        with self.assertRaises(machine.NotEnough) as said:
            machine.replay(PLAN, runs=10, offline=str(POOL), until="15:60")
        self.assertIn("--until wants a time", str(said.exception))

    def test_a_bad_flat_by_is_named_as_the_plans_not_as_until(self):
        with self.assertRaises(machine.NotEnough) as said:
            machine.replay(dict(PLAN, flat_by="16:00"), runs=10, offline=str(POOL))
        self.assertIn("the plan's flat_by 16:00 is after the 15:30 close", str(said.exception))

    def test_an_entry_bar_the_feed_left_empty_is_not_called_the_end_of_the_data(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            where = _changed_pool(tmp, lambda r: _blank(r, "2026-10-22", "10:10"))
            with self.assertRaises(machine.NotEnough) as said:
                machine.replay(PLAN, runs=10, offline=where)
        text = str(said.exception)
        self.assertIn("the feed sent no 10:10 bar, though it has later bars", text)
        self.assertNotIn("data stops", text)

    def test_the_newest_bar_waits_while_the_feed_is_behind_its_close(self):
        """The US pack reads bars as of the earlier of the clock and the feed's own last trade.
        The real file, with only its last-trade time moved to 10:14:50 on 2026-10-22."""
        import tempfile
        at = datetime(2026, 10, 22, 10, 15, 30, tzinfo=machine.clock.IST)
        lagging = int(datetime(2026, 10, 22, 10, 14, 50, tzinfo=machine.clock.IST).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            where = _changed_pool(tmp, lambda r: r["meta"].update(regularMarketTime=lagging))
            with self.assertRaises(machine.NotEnough) as said:
                machine.replay(PLAN, runs=10, offline=where, at=at)
            self.assertIn("the feed's last trade is 10:14:50, before the 10:10 bar's close",
                          str(said.exception))
            later = datetime(2026, 10, 22, 10, 16, 30, tzinfo=machine.clock.IST)
            found = machine.replay(PLAN, runs=10, offline=where, at=later)
        self.assertEqual(found["entry_time"], "10:15")
        # The unchanged file's feed time is after that close, so nothing waits.
        self.assertEqual(machine.replay(PLAN, runs=10, offline=str(POOL), at=at)["bars_left"], 61)

    def test_the_history_never_scores_a_day_across_a_missing_bar(self):
        """6 saved SBIN days have no 15:15 bar; the 15:10 close had been scored as 15:20's."""
        past = self.found["history"]
        gone = {g["date"]: g["why"] for g in past["left_out"]}
        self.assertIn("no 15:15 bar while the trade was open", gone.get("2026-08-03", ""))
        self.assertIn("no 11:45 bar before the range broke", gone.get("2026-10-15", ""))
        days = machine.days_of("SBIN", "5m", str(POOL))
        for trade in past["trades"]:
            self.assertNotIn(trade["date"], gone)
            have = {b["start"].strftime("%H:%M") for b in days[trade["date"]]}
            if trade["outcome"] == "close":
                self.assertIn("15:15", have, trade)
        self.assertEqual(past["triggered"], len(past["trades"]))
        said = "\n".join(machine.report(self.found))
        self.assertIn(f"{len(gone)} left out for a bar the feed never sent", said)

    def test_a_draw_counts_bars_on_the_clock_not_along_the_pool(self):
        """With a thin 10:10 left out, 'two bars back' from 10:15 had become 10:00."""
        history = machine.sessions("SBIN", "2026-10-22", offline=str(POOL))
        by_time = machine.pool(history)
        by_time.pop("10:10")
        picked = set()
        rng = random.Random(3)
        for _ in range(3000):
            picked.add(machine._draw("10:15", list(by_time), by_time, rng, 2)["at"])
        self.assertEqual(picked, {"10:05", "10:15", "10:20", "10:25"})

    def test_a_target_a_hair_off_one_r_is_not_called_a_coin_flip(self):
        """The US pack calls it 1R only within 0.005 of it."""
        self.assertIn("break-even", machine.verdict({"target": 50, "stop": 50}, 100, 1.008))
        self.assertIn("coin flip", machine.verdict({"target": 50, "stop": 50}, 100, 1.004))

    def test_an_average_r_that_rounds_to_nothing_adds_no_note(self):
        """The note had printed 'Average R is +0.000 all the same'."""
        counts = {"target": 10, "stop": 40, "neither": 50}
        self.assertEqual(machine.verdict_notes(counts, 50, 1.0, 100, 0.0, "15:20"),
                         ["50.0% of the replays hit neither by 15:20; the price at 15:20 decides "
                          "those."])
        self.assertEqual(len(machine.verdict_notes(counts, 50, 1.0, 100, 0.004, "15:20")), 2)

    def test_the_report_carries_the_us_packs_lines(self):
        said = "\n".join(machine.report(self.found))
        self.assertIn("SBIN buy, Thursday 2026-10-22: entry Rs 961.85 at 10:15 (bar 12)", said)
        self.assertTrue(any(line.lstrip().startswith("neither")
                            and line.endswith("still open at 15:20")
                            for line in said.splitlines()))
        self.assertIn("Repeat this run with --seed 19.", said)
        # The history comes before the verdict, as the US pack prints them.
        self.assertLess(said.index("What actually happened"), said.index(self.found["verdict"]))

    def test_the_result_says_where_its_bars_came_from(self):
        data = self.found["data"]
        self.assertEqual(self.found["source"], "offline")
        self.assertEqual(data["last_bar"], "2026-10-23 15:25 IST")
        self.assertEqual((data["interval"], data["range"]), ("5m", "60d"))

    def test_a_damaged_plan_is_refused(self):
        for change, words in ((dict(symbol="../SBIN"), "NSE symbol"),
                              (dict(symbol=["SBIN"]), "NSE symbol"),
                              (dict(entry=True), "each a price"),
                              (dict(opening_range={"minutes": 20.9}), "whole number")):
            with self.assertRaises(machine.NotEnough) as said:
                machine.checked(dict(PLAN, **change))
            self.assertIn(words, str(said.exception))
        self.assertEqual(machine.checked(dict(PLAN, side="long"))["side"], "buy")


@unittest.skipUnless(POOL.exists(), "fixture not saved")
class TheCommandLine(unittest.TestCase):
    def test_no_command_prints_the_help_as_the_us_pack_does(self):
        code, said = _run([])
        self.assertEqual(code, 1)
        self.assertIn("usage", said)

    def test_a_written_plan_with_no_date_is_refused(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            where = Path(tmp) / "plan.json"
            plan = dict(PLAN)
            plan.pop("date")
            where.write_text(json.dumps(plan), encoding="utf-8")
            code, said = _run(["run", "--plan", str(where), "--offline", str(POOL)])
            self.assertEqual(code, 2)
            self.assertIn("has no date", said)
            where.write_text(json.dumps(PLAN), encoding="utf-8")
            code, said = _run(["run", "--plan", str(where), "--offline", str(POOL),
                               "--flat-by", "15:00"])
            self.assertEqual(code, 2)
            self.assertIn("--flat-by describes a plan given as flags", said)

    def test_an_entry_of_zero_is_called_what_it_is(self):
        code, said = _run(["run", "--symbol", "SBIN", "--entry", "0", "--stop", "950",
                           "--date", "2026-10-22", "--entry-time", "10:15", "--offline",
                           str(POOL)])
        self.assertEqual(code, 2)
        self.assertIn("positive prices", said)

    def test_a_saved_chart_that_cannot_be_read_is_refused_in_one_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (("bad.json.gz", b"not gzip"), ("bad.json", b"{\"chart\": ")):
                where = Path(tmp) / name
                where.write_bytes(body)
                code, said = _run(["run", "--symbol", "SBIN", "--entry", "961.85", "--stop",
                                   "950.05", "--date", "2026-10-22", "--entry-time", "10:15",
                                   "--offline", str(where)])
                self.assertEqual(code, 2, said)
                self.assertIn("could not read", said)
                self.assertNotIn("Traceback", said)

    def test_a_damaged_saved_result_is_refused_not_a_traceback(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for body in ({"bands": [[1, 2]], "plan": {}}, {"bands": 5}, []):
                where = Path(tmp) / "tm.json"
                where.write_text(json.dumps(body), encoding="utf-8")
                code, said = _run(["chart", "--from", str(where)])
                self.assertEqual(code, 2, said)
                self.assertIn("not a time machine result", said)


@unittest.skipUnless(POOL.exists(), "fixture not saved")
class TheThirdSideBySidePass(unittest.TestCase):
    """Found by running the US pack's own code beside this one on complete sessions, and closed."""

    AT = datetime(2026, 10, 24, 9, 0, tzinfo=machine.clock.IST)

    def test_the_target_pays_its_r_to_four_places_and_the_report_says_that_r(self):
        """The US pack scores a target at round(R, 4) and prints it; this had printed 2.59R."""
        plan = dict(PLAN, stop=951.0, target=990.0)
        found = machine.replay(plan, runs=300, seed=19, offline=str(POOL), at=self.AT)
        r = round((990.0 - 961.85) / (961.85 - 951.0), 4)
        self.assertEqual(found["r_at_target"], r)
        self.assertIn(f"+{r:g}R at the target", "\n".join(machine.report(found)))
        history = machine.sessions("SBIN", "2026-10-22", offline=str(POOL))
        by_time = machine.pool(history)
        step = max(by_time["10:15"], key=lambda s: s["high"])
        how, paid = machine.one_replay(100.0, 99.0, 100.0 * step["high"], ["10:15"],
                                       {"10:15": [step]}, random.Random(1), 0)
        self.assertEqual(how, "target")
        self.assertEqual(paid, round((100.0 * step["high"] - 100.0) / 1.0, 4))

    def test_a_day_the_data_does_not_have_is_told_so_before_the_pool_is_counted(self):
        """The US pack checks the entry first: 2020 is not in the data, which is the answer."""
        with self.assertRaises(machine.NotEnough) as said:
            machine.replay(dict(PLAN, date="2020-01-01"), runs=10, offline=str(POOL))
        self.assertIn("the entry is not in the data", str(said.exception))

    def test_the_counts_behind_the_odds_are_kept(self):
        found = machine.replay(PLAN, runs=777, seed=3, offline=str(POOL), at=self.AT)
        counts = found["counts"]
        self.assertEqual(counts["target"] + counts["stop"] + counts["neither"], 777)
        self.assertEqual(found["green_percent"], round(counts["green"] / 777 * 100, 1))

    def test_the_history_averages_as_the_us_pack_does(self):
        """fmean and mean round apart in the last place: on ITC's real days, a 60-minute or-low
        1.5R rule to 15:20 averaged +0.124 with fmean and +0.123 with the US pack's mean."""
        itc = FIXTURES / "bars" / "ITC-60d5m.json.gz"
        history = machine.sessions("ITC", "2026-10-22", offline=str(itc))
        past = machine.history_replay(history, {"setup": "orb", "or_minutes": 60,
                                                "stop_rule": "or-low", "target_r": 1.5}, "15:20")
        import statistics
        self.assertEqual(past["avg_R"], round(statistics.mean(t["R"] for t in past["trades"]), 3))
        self.assertEqual(past["avg_R"], 0.123)

    def test_a_short_session_is_named_with_its_bars(self):
        """The US pack lists each short day with how many bars it had."""
        import tempfile

        def cut(result):
            for hhmm in [f"{h:02d}:{m:02d}" for h in range(12, 15) for m in range(0, 60, 5)]:
                _blank(result, "2026-09-01", hhmm)
        with tempfile.TemporaryDirectory() as tmp:
            found = machine.replay(PLAN, runs=50, offline=_changed_pool(tmp, cut), at=self.AT)
        days = machine.days_of("SBIN", "5m", str(POOL))
        self.assertEqual(found["short_sessions_left_out"],
                         [{"date": "2026-09-01", "bars": len(days["2026-09-01"]) - 36}])
        self.assertIn("Left out 1 short session(s): 2026-09-01.",
                      "\n".join(machine.report(found)))

    def test_the_bars_left_out_are_counted(self):
        at = datetime(2026, 10, 22, 10, 16, tzinfo=machine.clock.IST)
        found = machine.replay(PLAN, runs=20, offline=str(POOL), at=at)
        dropped = found["data"]["dropped"]
        days = machine.days_of("SBIN", "5m", str(POOL))
        later = sum(1 for day in days.values() for b in day if b["end"] > at)
        self.assertEqual(dropped["not_complete"], later)
        self.assertEqual(dropped["held_back"], 0)
        self.assertEqual(dropped["missing_in_past_sessions"],
                         sum(75 - len(day) for when, day in days.items() if when < "2026-10-22"))

    def test_a_flag_the_command_does_not_use_is_refused(self):
        code, said = _run(["run", "--from", "x.json", "--offline", str(POOL)])
        self.assertEqual(code, 2)
        self.assertIn("--from draws a saved result and goes with chart", said)
        code, said = _run(["chart", "--json", "--offline", str(POOL)])
        self.assertEqual(code, 2)
        self.assertIn("--json goes with run", said)

    def flags(self, *extra):
        return _run(["run", "--symbol", "SBIN", "--entry", "961.85", "--stop", "950.05",
                     "--date", "2026-10-22", "--entry-time", "10:15", "--offline", str(POOL),
                     "--replays", "50", "--now", "2026-10-24 09:00", *extra])

    def test_an_empty_until_or_target_is_refused_not_defaulted(self):
        code, said = self.flags("--until", "")
        self.assertEqual(code, 2)
        self.assertIn("--until wants a time", said)
        code, said = self.flags("--target", "")
        self.assertEqual(code, 2)
        self.assertIn("multiple like 2R", said)

    def test_a_flags_plan_is_flat_by_the_configs_flat_by_as_nse_exit_makes_it(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"flat_by": "15:00"}), encoding="utf-8")
            code, said = self.flags("--config", str(config))
            self.assertEqual(code, 0, said)
            self.assertIn("10:15 to 15:00", said)
            code, said = self.flags("--config", str(config), "--flat-by", "14:00")
            self.assertIn("10:15 to 14:00", said)
            config.write_text(json.dumps({"flat_by": "15:45"}), encoding="utf-8")
            code, said = self.flags("--config", str(config))
            self.assertEqual(code, 2)
            self.assertIn("flat_by in the config 15:45 is after the 15:30 close", said)
            code, said = self.flags("--config", str(Path(tmp) / "none.json"))
            self.assertEqual(code, 2)
            self.assertIn("no config at", said)
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps(PLAN), encoding="utf-8")
            code, said = _run(["run", "--plan", str(plan), "--config", str(config),
                               "--offline", str(POOL)])
            self.assertEqual(code, 2)
            self.assertIn("--config", said)

    def test_a_result_that_cannot_be_written_is_said_in_one_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            code, said = self.flags("--out", tmp)
        self.assertEqual(code, 2)
        self.assertIn("Cannot write the result", said)
        self.assertNotIn("Traceback", said)

    def test_a_saved_result_with_damaged_text_is_refused(self):
        import json
        import tempfile
        found = machine.replay(PLAN, runs=50, offline=str(POOL), at=self.AT)
        with tempfile.TemporaryDirectory() as tmp:
            for change in (dict(verdict_notes="abc"), dict(verdict=5), dict(band_times="x")):
                where = Path(tmp) / "tm.json"
                where.write_text(json.dumps(dict(found, **change), default=str),
                                 encoding="utf-8")
                code, said = _run(["chart", "--from", str(where)])
                self.assertEqual(code, 2, change)
                self.assertIn("not a time machine result", said)


class TickRounding(unittest.TestCase):
    def test_a_price_on_a_tick_never_moves(self):
        """950.05 / 0.05 is 19000.999... in floating point, and flooring it turned a 950.05 range
        low into a 950.00 stop. Every tick up to Rs 3,000 is checked."""
        self.assertEqual(machine.clock.to_tick(950.05, up=False), 950.05)
        for k in range(5000, 60000):
            price = round(k * 0.05, 2)
            self.assertEqual(machine.clock.to_tick(price, up=False), price)
            self.assertEqual(machine.clock.to_tick(price, up=True), price)



class TheFeedsDroppedBarsAreCounted(unittest.TestCase):
    """The bars the feed's reader threw away are reported, as the US pack reports them."""

    def test_sbin_60_days_has_113_bars_with_no_price(self):
        import gzip  # noqa: F401  (the fixture is read by bars.candles)
        dropped = {}
        machine.days_of("SBIN", offline=str(FIXTURES / "bars"), dropped=dropped)
        self.assertEqual(dropped.get("missing_price"), 113)

if __name__ == "__main__":
    unittest.main(verbosity=2)
