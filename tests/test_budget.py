"""Sizing: four limits, whole shares, and a refusal rather than a smaller order.

Runs on the real SBIN plan of 2026-10-22, loaded from the plan.json a real run of /nse-exit wrote.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import fixture, has, skill                            # noqa: E402
from nsetools import clock                                       # noqa: E402

budget = skill("nse-budget", "size")

PLAN = "chain/plan.json"
AT = clock.now("2026-10-22 11:00")


def rules(**over):
    """The defaults with a capital of 1 lakh, as config.example.json sets it. There is no default
    capital: the US pack refuses to size without one, and so does this."""
    settings = dict(budget.DEFAULTS, budget=100000.0)
    settings.update(over)
    return settings


@unittest.skipUnless(has(PLAN), "run capture.py")
class TheFourLimits(unittest.TestCase):
    def setUp(self):
        self.plan = fixture(PLAN)

    def test_the_plan_is_the_real_one(self):
        self.assertEqual(self.plan["symbol"], "SBIN")
        self.assertEqual(self.plan["entry"], 961.85)
        self.assertEqual(self.plan["stop"], 950.05)
        self.assertEqual(self.plan["date"], "2026-10-22")

    def test_the_smallest_limit_decides_the_size(self):
        """On a 962-rupee share with a 1 lakh account, the order cap binds first.

        Which limit decides is the useful output. Here it is not the risk limit at all.
        """
        sized = budget.size(self.plan, rules(), at=AT)
        self.assertEqual(sized["decided_by"], "max order")
        self.assertEqual(sized["quantity"], 41)
        self.assertEqual(sized["max_loss"], 483.80)      # 41 x 11.80

    def test_every_limit_is_reported_not_just_the_binding_one(self):
        sized = budget.size(self.plan, rules(), at=AT)
        self.assertEqual(set(sized["limits"]),
                         {"max loss", "position cap", "max order", "cash"})
        self.assertEqual(sized["limits"]["max loss"]["shares"], 84)
        self.assertEqual(sized["limits"]["position cap"]["shares"], 77)
        self.assertEqual(sized["limits"]["max order"]["shares"], 41)
        self.assertEqual(sized["limits"]["cash"]["shares"], 93)

    def test_a_wider_stop_buys_fewer_shares(self):
        """The stop decides the size. This is the whole idea."""
        wide = dict(self.plan, stop=900.0, target=1100.0)
        self.assertLess(budget.size(wide, rules(), at=AT)["quantity"],
                        budget.size(self.plan, rules(), at=AT)["quantity"])

    def test_the_max_loss_binds_once_the_stop_is_far_enough(self):
        wide = dict(self.plan, stop=900.0, target=1100.0)
        self.assertEqual(budget.size(wide, rules(), at=AT)["decided_by"], "max loss")

    def test_an_absolute_max_loss_overrides_the_percentage(self):
        settings = rules(max_loss_per_trade_rupees=200, max_order_rupees=10 ** 9)
        sized = budget.size(self.plan, settings, at=AT)
        self.assertEqual(sized["decided_by"], "max loss")
        self.assertEqual(sized["quantity"], 16)

    def test_whole_shares_only(self):
        """Indian equities have no fractional shares at any broker, so every limit floors."""
        sized = budget.size(self.plan, rules(), at=AT)
        self.assertIsInstance(sized["quantity"], int)

    def test_the_max_loss_is_the_shares_times_the_real_risk_a_share(self):
        sized = budget.size(self.plan, rules(), at=AT)
        self.assertAlmostEqual(sized["max_loss"],
                               sized["quantity"] * (self.plan["entry"] - self.plan["stop"]),
                               places=2)


@unittest.skipUnless(has(PLAN), "run capture.py")
class RefusedNotShrunk(unittest.TestCase):
    def setUp(self):
        self.plan = fixture(PLAN)

    def test_a_stop_too_far_for_the_max_loss_is_refused_not_resized(self):
        tiny = rules(max_loss_per_trade_rupees=5, max_order_rupees=10 ** 9)
        with self.assertRaises(budget.Refused) as caught:
            budget.size(self.plan, tiny, at=AT)
        self.assertIn("one share risks Rs 11.80", str(caught.exception))
        self.assertIn("Rs 5 max loss per trade", str(caught.exception))
        self.assertEqual(caught.exception.limits["max loss"]["shares"], 0)

    def test_a_poor_reward_to_risk_is_refused(self):
        poor = dict(self.plan, target=962.50)
        with self.assertRaises(budget.Refused) as caught:
            budget.size(poor, rules(), at=AT)
        self.assertIn("reward:risk", str(caught.exception))

    def test_a_plan_from_another_day_is_refused(self):
        """The real plan is dated 2026-10-22, so the next day it is no longer a plan."""
        with self.assertRaises(budget.Refused) as caught:
            budget.size(self.plan, rules(), at=clock.now("2026-10-23 10:20"))
        self.assertIn("2026-10-22", str(caught.exception))

    def test_a_blocked_symbol_is_refused(self):
        with self.assertRaises(budget.Refused):
            budget.size(self.plan, rules(blocked=["sbin"]), at=AT)

    def test_a_short_is_refused(self):
        with self.assertRaises(budget.Refused):
            budget.size(dict(self.plan, side="sell"), rules(), at=AT)

    def test_a_stop_above_the_entry_is_refused(self):
        with self.assertRaises(budget.Refused):
            budget.size(dict(self.plan, stop=970.0), rules(), at=AT)

    def test_an_order_under_the_minimum_is_refused(self):
        with self.assertRaises(budget.Refused) as caught:
            budget.size(self.plan, rules(max_order_rupees=1000, min_order_rupees=5000), at=AT)
        self.assertIn("minimum order", str(caught.exception))


@unittest.skipUnless(has(PLAN), "run capture.py")
class TheOrderItWrites(unittest.TestCase):
    def setUp(self):
        self.plan = fixture(PLAN)
        self.order = budget.size(self.plan, rules(), at=AT)["order"]

    def test_it_matches_the_order_a_real_run_wrote(self):
        """The saved orders.json came out of the chain. Sizing the same plan must reproduce it."""
        saved = fixture("chain/orders.json")["orders"][0]
        self.assertEqual(self.order["quantity"], saved["quantity"])
        self.assertEqual(self.order["stop_loss_price"], saved["stop_loss_price"])
        self.assertEqual(self.order["take_profit_price"], saved["take_profit_price"])

    def test_it_is_a_bracket_with_both_exits_attached(self):
        self.assertEqual(self.order["order_class"], "bracket")
        self.assertEqual(self.order["stop_loss_price"], self.plan["stop"])
        self.assertEqual(self.order["take_profit_price"], self.plan["target"])

    def test_it_is_an_indian_order_not_an_american_one(self):
        self.assertEqual(self.order["exchange"], "NSE")
        self.assertEqual(self.order["product"], "MIS")
        self.assertEqual(self.order["validity"], "DAY")

    def test_a_market_order_carries_no_price(self):
        self.assertIsNone(self.order["price"])

    def test_a_limit_order_carries_the_limit(self):
        order = budget.size(self.plan, rules(), limit=960.0, at=AT)["order"]
        self.assertEqual(order["order_type"], "LIMIT")
        self.assertEqual(order["price"], 960.0)

    def test_it_says_which_limit_decided_the_size(self):
        self.assertIn("max order", self.order["why"])

    def test_paper_is_carried_through(self):
        self.assertTrue(self.order["paper"])


@unittest.skipUnless(has("chain/prices.json"), "run capture.py")
class AFreshPriceOnlyEverShrinksTheSize(unittest.TestCase):
    """A price that ran up means more risk at the same stop. A lower one changes nothing."""

    def test_a_higher_price_means_more_risk_a_share_and_a_smaller_risk_limit(self):
        """At 965 instead of 961.85 the same stop is further away, so the max-loss limit falls.

        The ORDER cap still allows 41 either way on this account, which is why all four limits
        are printed: the binding one does not move just because the risk did.
        """
        plan = fixture(PLAN)
        higher, why = budget.entry_price(plan, 965.0, AT)
        self.assertEqual(higher, 965.0)
        self.assertIn("965", why)
        at_plan = budget.size(plan, rules(), at=AT)
        at_higher = budget.size(plan, rules(), limit=965.0, at=AT)
        self.assertGreater(at_higher["risk_per_share"], at_plan["risk_per_share"])
        self.assertLess(at_higher["limits"]["max loss"]["shares"],
                        at_plan["limits"]["max loss"]["shares"])

    def test_a_price_that_ran_too_far_is_refused_because_the_move_already_happened(self):
        """At 975 the real target of 985.55 is only 10.55 away against 25 of risk.

        Reward:risk 0.42, so the trade the plan described no longer exists. Chasing it is the
        mistake this refusal is for.
        """
        with self.assertRaises(budget.Refused) as caught:
            budget.size(fixture(PLAN), rules(), limit=975.0, at=AT)
        self.assertIn("reward:risk", str(caught.exception))

    def test_a_lower_price_never_shrinks_the_risk_so_the_plan_stands(self):
        """A price that fell is not a cheaper trade, so the plan's entry is still what is sized."""
        plan = fixture(PLAN)
        used, why = budget.entry_price(plan, None, AT)
        self.assertEqual(used, plan["entry"])
        self.assertIn("plan's entry", why)

    def test_the_real_saved_price_is_a_quote_with_its_own_time(self):
        saved = fixture("chain/prices.json")
        self.assertIn("as_of", saved)
        self.assertIn("prices", saved)


@unittest.skipUnless(has(PLAN), "run capture.py")
class SplittingCapitalAcrossAList(unittest.TestCase):
    """A different job: no stop, so only the weights and the limits decide.

    The algorithm is the US pack's, and the first test proves it: on that pack's own example
    file it gives that pack's own documented numbers, to the unit.
    """

    def test_the_reference_example_comes_out_exactly(self):
        """Berkshire's top five, budget 5,000, 10% buffer, 25% cap: AAPL 1,250 capped, AXP
        1,119, GOOGL 823, KO 708, BAC 600 -- the figures the US pack's own guide prints."""
        weights = {"AAPL": 30.67, "AXP": 23.85, "GOOGL": 17.56, "KO": 15.11, "BAC": 12.80}
        out = budget.split(weights, rules(budget=5000, cash_buffer_pct=10, max_position_pct=25,
                                          min_order_rupees=5))
        got = {s["symbol"]: s["rupees"] for s in out["slices"]}
        self.assertEqual(got, {"AAPL": 1250, "AXP": 1119, "GOOGL": 823, "KO": 708, "BAC": 600})
        self.assertEqual(out["capped"], ["AAPL"])
        self.assertEqual(out["idle"], 0)

    def test_weights_are_renormalised_and_the_buffer_comes_off_the_top(self):
        out = budget.split({"SBIN": 40, "ITC": 60}, rules())
        self.assertEqual(out["budget_after_buffer"], 90000.0)
        self.assertEqual(sum(s["rupees"] for s in out["slices"]), 90000)

    def test_the_cap_is_a_share_of_the_whole_budget_not_of_what_is_left(self):
        """75% of a 1 lakh budget is 75,000 -- not 75% of the 90,000 after the buffer."""
        out = budget.split({"RELIANCE": 90, "ITC": 5, "SBIN": 5}, rules())
        got = {s["symbol"]: s["rupees"] for s in out["slices"]}
        self.assertEqual(out["cap"], 75000.0)
        self.assertEqual(got["RELIANCE"], 75000)
        self.assertEqual(got["ITC"] + got["SBIN"], 15000)
        self.assertEqual(out["capped"], ["RELIANCE"])

    def test_blocked_names_come_out_before_the_top_n(self):
        out = budget.split({"RELIANCE": 40, "ITC": 25, "TCS": 20, "SBIN": 15},
                           rules(blocked=["ITC"]), top=3)
        self.assertEqual(out["blocked"], ["ITC"])
        self.assertEqual({s["symbol"] for s in out["slices"]}, {"RELIANCE", "TCS", "SBIN"})

    def test_names_outside_the_top_n_are_reported(self):
        out = budget.split({"A": 50, "B": 30, "C": 20}, rules(), top=2)
        self.assertTrue(any("outside the top 2" in d for d in out["dropped"]))

    @unittest.skipUnless(has("chain/prices.json"), "run capture.py")
    def test_with_real_prices_whole_shares_and_the_cash_is_used_up(self):
        """Floored, then topped up one share at a time, so the leftover is under one share."""
        prices = fixture("chain/prices.json")["prices"]
        out = budget.split({s: 20 for s in prices}, rules(), prices)
        for row in out["slices"]:
            self.assertEqual(row["rupees"], round(row["shares"] * prices[row["symbol"]], 2))
        self.assertLess(out["idle"], max(prices.values()))

    @unittest.skipUnless(has("chain/prices.json"), "run capture.py")
    def test_a_missing_price_is_a_refusal_that_names_it(self):
        prices = fixture("chain/prices.json")["prices"]
        with self.assertRaises(budget.Refused) as caught:
            budget.split({"SBIN": 50, "TCS": 50}, rules(), prices)
        self.assertIn("TCS", str(caught.exception))
        self.assertIn("quote TCS --write", str(caught.exception))

    @unittest.skipUnless(has("chain/prices.json"), "run capture.py")
    def test_a_name_that_cannot_buy_one_share_is_dropped_and_its_money_re_split(self):
        """RELIANCE at 1,190 cannot be bought with a small slice; SUZLON at 37 can, and gets the
        whole of what is left rather than half of it sitting idle."""
        prices = fixture("chain/prices.json")["prices"]
        out = budget.split({"RELIANCE": 50, "SUZLON": 50}, rules(cash_buffer_pct=98.4), prices)
        self.assertEqual([s["symbol"] for s in out["slices"]], ["SUZLON"])
        self.assertTrue(any("RELIANCE" in d for d in out["dropped"]))
        self.assertLess(out["idle"], prices["SUZLON"])

    def test_weights_adding_to_nothing_are_refused(self):
        with self.assertRaises(budget.Refused):
            budget.split({"SBIN": 0}, rules())

    def test_weight_orders_are_whole_share_delivery_buys_with_no_stop(self):
        prices = {"SBIN": 958.6, "SUZLON": 37.4}
        out = budget.split({"SBIN": 50, "SUZLON": 50}, rules(), prices)
        orders = budget.weight_orders(out, "test")
        for order in orders:
            self.assertEqual(order["product"], "CNC")
            self.assertIsInstance(order["quantity"], int)
            self.assertNotIn("stop_loss_price", order)

    def test_the_orders_file_says_what_it_is(self):
        doc = budget.orders_file([], rules(), "here")
        for key in ("created_by", "created_at", "status", "paper", "note", "orders"):
            self.assertIn(key, doc)
        self.assertEqual(doc["status"], "proposed")


def cli(argv):
    """Run size.py's main as the command line would, and what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = budget.main(argv)
    return code, out.getvalue()


@unittest.skipUnless(has(PLAN) and has("chain/prices.json"), "run capture.py")
class TheThirdPass(unittest.TestCase):
    """Found by sizing the same inputs through the US pack's own functions, and closed.

    Every case is the real SBIN plan or the real prices file with ONE fact changed.
    """

    def setUp(self):
        self.plan = fixture(PLAN)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.none = str(self.home / "none.json")

    def tearDown(self):
        budget.PRICES_PATH = budget.PORTFOLIO_PATH = budget.CONFIG_PATH = None
        self.tmp.cleanup()

    def save(self, name, body):
        path = self.home / name
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return str(path)

    def sized(self, *extra, plan=None):
        where = self.save("plan.json", plan if plan is not None else self.plan)
        prices = self.save("prices.json", fixture("chain/prices.json"))
        return cli(["--plan", where, "--now", "2026-10-22 10:20", "--prices", prices,
                    "--portfolio", self.none, "--budget", "100000", *extra])

    # 1. reward:risk in whole paise
    def test_a_trade_of_exactly_one_r_is_not_refused_as_under_one(self):
        """Target 973.65 is 961.85 + 11.80. In floating point that is 0.9999999999999903R, and
        the minimum of 1 refused it. The US pack, in whole cents, sizes it."""
        one_r = dict(self.plan, target=973.65)
        self.assertLess((973.65 - 961.85) / (961.85 - 950.05), 1)    # the trap is real
        sized = budget.size(one_r, rules(), at=AT)
        self.assertEqual(sized["reward_risk"], 1.0)
        self.assertEqual(sized["quantity"], 41)

    # 2. a price the exchange does not take is refused, never moved
    def test_an_off_tick_limit_is_refused_naming_the_nearest_ticks(self):
        code, said = self.sized("--limit", "961.83")
        self.assertEqual(code, 2)
        self.assertIn("961.83 is not on an NSE tick", said)
        self.assertIn("Rs 961.80 and Rs 961.85", said)
        code, said = self.sized("--limit", "961.837")
        self.assertIn("961.837", said)                    # said as given, not as 961.84

    def test_an_off_tick_stop_or_target_is_refused(self):
        for key, value in (("stop", 950.03), ("target", 985.42)):
            with self.assertRaises(budget.Refused) as caught:
                budget.size(dict(self.plan, **{key: value}), rules(), at=AT)
            self.assertIn("not on an NSE tick", str(caught.exception))

    def test_a_price_on_the_tick_still_sizes(self):
        self.assertEqual(budget.size(self.plan, rules(), limit=961.80, at=AT)["quantity"], 41)

    # 4. bad files and bad numbers are refusals, not tracebacks
    def test_a_broken_plan_is_a_refusal_not_a_crash(self):
        no_symbol = dict(self.plan)
        no_symbol.pop("symbol")
        no_entry = dict(self.plan)
        no_entry.pop("entry")
        for plan, extra, words in [(no_symbol, (), "no symbol"), ([1, 2], (), "not a JSON object"),
                                   ("not json", (), "not valid JSON"),
                                   (no_entry, ("--limit",), "positive entry")]:
            code, said = self.sized(*extra, plan=plan)
            self.assertEqual(code, 2, said)
            self.assertIn(words, said)
            self.assertNotIn("Traceback", said)

    def test_a_config_value_that_is_not_a_number_is_named(self):
        # null is "not set" (the US pack skips the limit); text that is not a number is refused.
        for key, value in (("max_order_rupees", "abc"), ("max_position_pct", "abc"),
                           ("budget", float("nan")), ("min_reward_risk", "x")):
            with self.assertRaises(budget.Refused) as caught:
                budget.size(self.plan, rules(**{key: value}), at=AT)
            self.assertIn(key, str(caught.exception))

    def test_a_number_written_as_text_is_read_as_the_number(self):
        sized = budget.size(dict(self.plan, entry="961.85"), rules(max_position_pct="75"), at=AT)
        self.assertEqual(sized["quantity"], 41)

    # 5. both shapes of a weights file
    def test_the_list_form_of_a_weights_file_is_read(self):
        prices = self.save("prices.json", fixture("chain/prices.json"))
        listed = self.save("t.json", {"weights": [{"symbol": "SBIN", "weight": 40},
                                                  {"ticker": "ITC", "weight": 60}]})
        keyed = self.save("k.json", {"weights": {"SBIN": 40, "ITC": 60}})
        a = cli(["--budget", "100000", "--targets", listed, "--prices", prices, "--json"])
        b = cli(["--budget", "100000", "--targets", keyed, "--prices", prices, "--json"])
        self.assertEqual(a[0], 0, a[1])
        self.assertEqual(json.loads(a[1])["slices"], json.loads(b[1])["slices"])

    # 6. a price that is not a price
    def test_a_negative_or_nan_price_is_refused_not_looped_on(self):
        for bad in (-5, "NaN"):
            real = fixture("chain/prices.json")
            real["prices"]["ITC"] = bad
            body = json.dumps(real).replace('"NaN"', "NaN")
            code, said = cli(["--budget", "100000", "--weights", "SBIN=40,ITC=60",
                              "--prices", self.save("p.json", body)])
            self.assertEqual(code, 2)
            self.assertIn("a price has to be a number above 0", said)

    def test_a_price_written_as_text_or_in_small_letters_is_used(self):
        real = fixture("chain/prices.json")
        real["prices"] = {"sbin": "958.6", "ITC": 257.9}
        code, said = cli(["--budget", "100000", "--weights", "SBIN=40,ITC=60", "--json",
                          "--prices", self.save("p.json", real)])
        self.assertEqual(code, 0, said)
        self.assertTrue(all(s["shares"] for s in json.loads(said)["slices"]))

    # 7. a split uses the saved prices, as plan mode does
    def test_a_split_uses_nse_prices_json_when_it_is_there(self):
        (self.home / "nse").mkdir()
        self.save("nse/prices.json", fixture("chain/prices.json"))
        here = os.getcwd()
        os.chdir(self.home)
        try:
            code, said = cli(["--budget", "100000", "--weights", "SBIN=40,ITC=60", "--whole-shares", "--json"])
            bare = cli(["--budget", "100000", "--weights", "SBIN=40,ITC=60", "--json"])
        finally:
            os.chdir(here)
        self.assertEqual(code, 0, said)
        self.assertTrue(json.loads(said)["whole_shares"])
        self.assertTrue(json.loads(bare[1])["whole_shares"])

    def test_whole_shares_with_no_prices_anywhere_is_refused(self):
        here = os.getcwd()
        os.chdir(self.home)
        try:
            code, said = cli(["--budget", "100000", "--weights", "SBIN=40,ITC=60", "--whole-shares"])
        finally:
            os.chdir(here)
        self.assertEqual(code, 2)
        self.assertIn("needs a price", said)

    # 8. the report shows what is being proposed
    def test_the_report_shows_the_exits_the_order_and_the_flat_by_time(self):
        code, said = self.sized()
        self.assertEqual(code, 0, said)
        self.assertIn("Stop Rs 950.05: opening-range low", said)
        self.assertIn("Target Rs 985.45: 2R", said)
        self.assertIn("buy 41 SBIN, MARKET, MIS, day, take-profit Rs 985.45, "
                      "stop-loss Rs 950.05", said)
        self.assertIn("Flat by 15:20 IST", said)
        code, said = self.sized("--limit", "960.00")
        self.assertIn("LIMIT Rs 960.00, MIS", said)

    # 9. the file is dated by the pinned clock
    def test_the_orders_file_is_stamped_with_the_pinned_time(self):
        out = str(self.home / "orders.json")
        code, said = self.sized("--write", out)
        self.assertEqual(code, 0, said)
        doc = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertEqual(doc["created_at"], "2026-10-22T04:50:00+00:00")   # 10:20 IST

    # 10. the real reason, not "under your minimum order" with no minimum set
    def test_a_split_nothing_can_buy_says_one_share_costs_more_than_the_slice(self):
        real = fixture("chain/prices.json")
        real["prices"]["RELIANCE"] = 125000.0
        code, said = cli(["--budget", "100000", "--weights", "RELIANCE=1", "--prices", self.save("p.json", real)])
        self.assertEqual(code, 2)
        # 75% of the 1 lakh: the slice is the cap, not the 90,000 after the buffer
        self.assertIn("one share costs Rs 125,000, more than its slice of Rs 75,000", said)
        self.assertNotIn("minimum order", said)

    # the smaller ones
    def test_a_price_with_no_quote_time_of_its_own_is_not_used(self):
        """The real prices file has a time for the file and none per symbol. Its SBIN, raised
        above the plan, is unverified, so the plan's entry stands and that is said."""
        real = fixture("chain/prices.json")
        real["prices"]["SBIN"] = 963.0
        real["as_of"] = "2026-10-22T10:18:00+05:30"         # the file is fresh; SBIN is undated
        budget.PRICES_PATH = self.save("p.json", real)
        price, why = budget.entry_price(self.plan, None, AT)
        self.assertEqual(price, 961.85)
        self.assertIn("no quote time", why)

    def test_buy_in_capitals_is_a_buy(self):
        self.assertEqual(budget.size(dict(self.plan, side="BUY"), rules(), at=AT)["quantity"], 41)

    def test_top_below_one_is_refused(self):
        code, said = cli(["--budget", "100000", "--weights", "SBIN=40,ITC=60", "--top", "0"])
        self.assertEqual(code, 2)
        self.assertIn("1 or more", said)

    def test_rupees_keep_their_paise_where_they_change_the_answer(self):
        """A 99,999.99 budget leaves 89,999.99 after the buffer: printed as such, not 90,000."""
        sized = budget.size(self.plan, rules(budget=99999.99), at=AT)
        self.assertIn("= Rs 89,999.99 /", sized["limits"]["cash"]["math"])

    def test_a_dropped_slice_says_rupees(self):
        out = budget.split({"SBIN": 99, "ITC": 1},
                           rules(min_order_rupees=5000, max_position_pct=100))
        self.assertIn("its slice of Rs 900 is under your Rs 5,000 minimum order", out["dropped"][0])


@unittest.skipUnless(has(PLAN) and has("chain/prices.json"), "run capture.py")
class TheFourthPass(unittest.TestCase):
    """Found by the fourth side-by-side pass against the US pack, and closed.

    Every case is the real SBIN plan, the real prices file or the real config shape with ONE
    fact changed.
    """

    def setUp(self):
        self.plan = fixture(PLAN)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.none = str(self.home / "none.json")

    def tearDown(self):
        budget.PRICES_PATH = budget.PORTFOLIO_PATH = budget.CONFIG_PATH = None
        self.tmp.cleanup()

    def save(self, name, body):
        path = self.home / name
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return str(path)

    def sized(self, *extra, plan=None, config=None):
        where = self.save("plan.json", plan if plan is not None else self.plan)
        rules_at = self.save("config.json", config if config is not None else {"capital": 100000})
        prices = self.save("prices.json", fixture("chain/prices.json"))
        return cli(["--plan", where, "--now", "2026-10-22 10:20", "--prices", prices,
                    "--portfolio", self.none, "--config", rules_at, *extra])

    def in_home(self, argv):
        here = os.getcwd()
        os.chdir(self.home)
        try:
            return cli(argv)
        finally:
            os.chdir(here)

    # the report
    def test_every_limit_is_printed_in_full_and_the_deciding_one_marked(self):
        """The cash row was cut to "...cash ..." by a fixed column, hiding its figure."""
        code, said = self.sized()
        self.assertEqual(code, 0, said)
        self.assertIn("Rs 100,000 capital less the Rs 10,000 cash buffer = Rs 90,000 / "
                      "Rs 961.85", said)
        self.assertIn("75% of Rs 100,000 = Rs 75,000 / Rs 961.85", said)
        row = next(line for line in said.splitlines() if line.startswith("max order"))
        self.assertIn("<- decides", row)
        self.assertEqual(sum("<- decides" in line for line in said.splitlines()), 1)

    def test_every_report_ends_with_the_disclaimer(self):
        tiny = {"capital": 100000, "max_loss_per_trade_rupees": 5}
        for extra, config in (((), None), ((), tiny)):
            code, said = self.sized(*extra, config=config)
            self.assertIn("Education and tooling, not financial advice.", said)
        prices = self.save("p.json", fixture("chain/prices.json"))
        code, said = cli(["--budget", "100000", "--weights", "SBIN=40,ITC=60", "--prices", prices])
        self.assertIn("Education and tooling, not financial advice.", said)

    def test_the_setup_is_named_in_words_and_the_max_loss_by_its_rupees(self):
        order = budget.size(self.plan, rules(), at=AT)["order"]
        self.assertTrue(order["why"].startswith("opening-range breakout on SBIN: 41 shares, "
                                                "bound by your max order"))
        wide = budget.size(dict(self.plan, stop=900.0, target=1100.0), rules(), at=AT)
        self.assertIn("bound by your Rs 1,000 max loss per trade", wide["order"]["why"])

    def test_plan_text_that_is_not_text_is_printed_not_a_crash(self):
        """An entry_time of 5 stopped the report with a TypeError."""
        code, said = self.sized(plan=dict(self.plan, entry_time=5, setup=None))
        self.assertEqual(code, 0, said)
        self.assertIn("entry Rs 961.85 at 5", said)
        self.assertNotIn("Traceback", said)

    # no capital is a refusal, as the US pack refuses it: never a default passed off as theirs
    def test_no_capital_anywhere_is_refused_not_sized_on_a_default(self):
        code, said = self.sized(config={})
        self.assertEqual(code, 2, said)
        self.assertIn("no capital to size against", said)
        self.assertNotIn("<- decides", said)                # nothing was sized
        code, said = self.sized("--json", config={})
        self.assertIn("no capital to size against", json.loads(said)["refused"])
        code2, said2 = self.sized("--budget", "100000", config={})
        self.assertEqual(code2, 0, said2)
        code3, said3 = self.sized(config={"capital": 100000})
        self.assertEqual(code3, 0, said3)
        # a budget left empty falls back to the capital beside it, never to nothing
        code4, said4 = self.sized(config={"budget": None, "capital": 100000})
        self.assertEqual(code4, 0, said4)

    def test_a_split_with_no_capital_is_refused(self):
        code, said = self.in_home(["--weights", "SBIN=40,ITC=60"])
        self.assertEqual(code, 2, said)
        self.assertIn("no capital to size against", said)

    def test_a_portfolio_is_not_taken_for_the_capital(self):
        """The US pack sizes against a budget and only caps it by the account's cash."""
        held = self.save("pf.json", {"cash": 30000})
        where = self.save("plan.json", self.plan)
        code, said = cli(["--plan", where, "--now", "2026-10-22 10:20", "--portfolio", held,
                          "--prices", self.save("p.json", fixture("chain/prices.json")),
                          "--config", self.save("c.json", {})])
        self.assertEqual(code, 2, said)
        self.assertIn("no capital to size against", said)

    # prices
    def test_a_named_prices_file_that_is_not_there_is_refused(self):
        """As the US pack refuses it: the user named a file to check the live price against."""
        budget.PRICES_PATH = self.none
        with self.assertRaises(budget.Refused) as caught:
            budget.entry_price(self.plan, None, AT)
        self.assertIn("no prices at", str(caught.exception))
        # a limit order looks at no price, so the missing file does not matter there
        self.assertEqual(budget.entry_price(self.plan, 961.85, AT)[0], 961.85)

    def test_a_prices_file_with_no_table_is_said(self):
        real = fixture("chain/prices.json")
        real["prices"] = []
        budget.PRICES_PATH = self.save("p.json", real)
        price, why = budget.entry_price(self.plan, None, AT)
        self.assertEqual(price, 961.85)
        self.assertIn("no table of prices", why)

    def test_a_fresh_price_that_is_used_says_when_it_was_quoted(self):
        real = fixture("chain/prices.json")
        real["prices"]["SBIN"] = 963.0
        real["detail"] = {"SBIN": {"at": "2026-10-22T10:48:00+05:30"}}
        budget.PRICES_PATH = self.save("p.json", real)
        price, why = budget.entry_price(self.plan, None, AT)
        self.assertEqual(price, 963.0)
        self.assertIn("quoted 10:48 IST", why)

    def test_a_fresh_price_below_the_plan_is_said_and_the_plan_stands(self):
        """The real SBIN quote of 958.60, given a time of its own: below the plan's 961.85."""
        real = fixture("chain/prices.json")
        real["detail"] = {"SBIN": {"at": "2026-10-22T10:48:00+05:30"}}
        budget.PRICES_PATH = self.save("p.json", real)
        price, why = budget.entry_price(self.plan, None, AT)
        self.assertEqual(price, 961.85)
        self.assertIn("the live Rs 958.60, quoted 10:48 IST, is not above it", why)

    def test_a_prices_file_without_the_symbol_says_so(self):
        real = fixture("chain/prices.json")
        del real["prices"]["SBIN"]
        budget.PRICES_PATH = self.save("p.json", real)
        price, why = budget.entry_price(self.plan, None, AT)
        self.assertEqual(price, 961.85)
        self.assertIn("no SBIN price in p.json", why)

    # limits the US pack skips when they are not set
    def test_a_max_order_of_zero_or_null_is_no_limit_as_in_the_us_pack(self):
        for value in (0, None):
            sized = budget.size(self.plan, rules(max_order_rupees=value), at=AT)
            self.assertNotIn("max order", sized["limits"])
            self.assertEqual(sized["decided_by"], "position cap")
            self.assertEqual(sized["quantity"], 77)

    def test_a_position_cap_of_zero_is_no_limit_and_a_negative_one_is_refused(self):
        sized = budget.size(self.plan, rules(max_position_pct=0, max_order_rupees=0), at=AT)
        self.assertEqual((sized["decided_by"], sized["quantity"]), ("max loss", 84))
        with self.assertRaises(budget.Refused) as caught:
            budget.size(self.plan, rules(max_position_pct=-5), at=AT)
        self.assertIn("cannot be below 0", str(caught.exception))

    def test_a_null_minimum_order_or_reward_risk_is_no_floor(self):
        poor = dict(self.plan, target=962.50)          # 0.06R: refused under the default 1
        sized = budget.size(poor, rules(min_reward_risk=None, min_order_rupees=None), at=AT)
        self.assertEqual(sized["quantity"], 41)

    # the blocked list
    def test_a_blocked_list_written_as_one_string_still_blocks(self):
        """A string was walked letter by letter, so "SBIN" blocked S, B, I and N, never SBIN."""
        with self.assertRaises(budget.Refused):
            budget.size(self.plan, rules(blocked="SBIN"), at=AT)
        out = budget.split({"SBIN": 40, "ITC": 60}, rules(blocked="ITC, TCS"))
        self.assertEqual(out["blocked"], ["ITC"])

    def test_a_number_in_the_blocked_list_is_not_a_crash(self):
        out = budget.split({"SBIN": 40, "ITC": 60}, rules(blocked=[5]))
        self.assertEqual(len(out["slices"]), 2)
        with self.assertRaises(budget.Refused):
            budget.size(self.plan, rules(blocked=5), at=AT)

    # the portfolio
    def test_a_portfolio_whose_cash_is_not_a_number_is_refused(self):
        for cash in ("abc", None):
            budget.PORTFOLIO_PATH = self.save("pf.json", {"cash": cash})
            with self.assertRaises(budget.Refused) as caught:
                budget.size(self.plan, rules(), at=AT)
            self.assertIn("no cash figure", str(caught.exception))

    # the clock
    def test_today_is_the_indian_date_whatever_zone_the_clock_is_in(self):
        """01:30 IST on the 22nd is 20:00 UTC on the 21st: still the plan's day."""
        from datetime import datetime, timezone
        utc = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(budget.size(self.plan, rules(), at=utc)["quantity"], 41)

    # the orders file
    def test_json_with_write_is_still_json_and_says_where(self):
        out = str(self.home / "orders.json")
        code, said = self.sized("--write", out, "--json")
        self.assertEqual(code, 0, said)
        self.assertEqual(json.loads(said)["wrote"], out)

    def test_the_orders_file_is_written_whole_and_names_the_plan(self):
        out = self.home / "orders.json"
        code, said = self.sized("--write", str(out))
        self.assertEqual(code, 0, said)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(doc["source"].endswith("(SBIN orb 2026-10-22)"))
        self.assertFalse((self.home / "orders.json.tmp").exists())
        self.assertIn("Wrote 1 proposed bracket order to", said)

    def test_writing_to_a_folder_is_a_refusal_not_a_crash(self):
        code, said = self.sized("--write", str(self.home))
        self.assertEqual(code, 2)
        self.assertIn("could not write the orders", said)

    def test_limit_plan_is_the_plans_entry(self):
        code, said = self.sized("--limit", "plan")
        self.assertEqual(code, 0, said)
        self.assertIn("LIMIT Rs 961.85", said)

    # the split
    def test_a_split_reports_what_is_invested_and_left_idle(self):
        prices = self.save("p.json", fixture("chain/prices.json"))
        code, said = cli(["--weights", "SBIN=40,ITC=60", "--prices", prices, "--budget", "100000"])
        self.assertEqual(code, 0, said)
        self.assertIn("Budget Rs 100,000, cash buffer 10% = Rs 10,000, investing Rs 90,000, "
                      "cap 75% = Rs 75,000 per stock", said)
        self.assertIn("Rs 54,416.90", said)                  # 211 ITC at 257.90, not 54416.9
        self.assertIn("invested     Rs 89,885.10", said)
        self.assertIn("left idle    Rs 114.90", said)
        self.assertIn("buy 211 ITC, MARKET, CNC, day", said)

    def test_a_split_on_prices_with_no_quote_time_says_they_are_not_live(self):
        """The real prices file has no per-symbol time: the split is worked from it, and said."""
        prices = self.save("p.json", fixture("chain/prices.json"))
        code, said = cli(["--budget", "100000", "--weights", "SBIN=40,ITC=60", "--prices", prices, "--json"])
        out = json.loads(said)
        self.assertTrue(any(n.startswith("Not live prices: ITC, SBIN") for n in out["notes"]))
        self.assertTrue(all("undated" in o["why"] for o in out["orders"]))

    def test_a_split_on_fresh_prices_shows_their_time_and_no_warning(self):
        real = fixture("chain/prices.json")
        real["detail"] = {s: {"at": "2026-10-22T10:18:00+05:30"} for s in ("SBIN", "ITC")}
        prices = self.save("p.json", real)
        code, said = cli(["--budget", "100000", "--weights", "SBIN=40,ITC=60", "--prices", prices,
                          "--now", "2026-10-22 10:20", "--budget", "100000"])
        self.assertEqual(code, 0, said)
        self.assertNotIn("Not live prices", said)
        self.assertIn("quoted 2026-10-22 10:18 IST", said)

    def test_a_capped_order_says_its_cap(self):
        prices = fixture("chain/prices.json")["prices"]
        out = budget.split({"RELIANCE": 90, "ITC": 5, "SBIN": 5}, rules(), prices)
        why = next(o["why"] for o in budget.weight_orders(out) if o["symbol"] == "RELIANCE")
        self.assertIn("capped at 75% of the Rs 100,000 budget", why)

    def test_the_names_in_a_list_weights_file_are_shown(self):
        prices = self.save("p.json", fixture("chain/prices.json"))
        listed = self.save("t.json", {"weights": [
            {"symbol": "SBIN", "weight": 40, "name": "State Bank of India"},
            {"ticker": "ITC", "weight": 60}]})
        code, said = cli(["--budget", "100000", "--targets", listed, "--prices", prices])
        self.assertEqual(code, 0, said)
        self.assertIn("State Bank of India", said)

    def test_a_split_refusal_under_json_is_json(self):
        code, said = cli(["--budget", "100000", "--weights", "SBIN=x", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("not a number", json.loads(said)["refused"])

    def test_an_empty_weights_list_is_a_split_with_nothing_not_the_plan(self):
        code, said = self.in_home(["--budget", "100000", "--weights", ""])
        self.assertEqual(code, 2)
        self.assertIn("nothing to split", said)
        self.assertNotIn("blocked list", said)        # no blocked list was involved

    def test_the_skill_md_weights_example_gives_the_numbers_it_says(self):
        """RELIANCE 75,000 capped, ITC and SBIN 7,500 each: not "12.5% each" as it said."""
        out = budget.split({"RELIANCE": 90, "ITC": 5, "SBIN": 5}, rules())
        self.assertEqual({s["symbol"]: s["rupees"] for s in out["slices"]},
                         {"RELIANCE": 75000, "ITC": 7500, "SBIN": 7500})
        text = (Path(budget.__file__).parent / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("RELIANCE Rs 75,000", text)
        self.assertIn("ITC and SBIN Rs 7,500 each", text)


@unittest.skipUnless(has(PLAN) and has("chain/prices.json"), "run capture.py")
class TheFifthPass(unittest.TestCase):
    """Found by the fifth side-by-side pass against the US pack (40,000 random cases compared
    number for number, plus hostile inputs), and closed. Real plan, real prices, one fact
    changed each time."""

    def setUp(self):
        self.plan = fixture(PLAN)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.prices = self.save("prices.json", fixture("chain/prices.json"))

    def tearDown(self):
        budget.PRICES_PATH = budget.PORTFOLIO_PATH = budget.CONFIG_PATH = None
        self.tmp.cleanup()

    def save(self, name, body):
        path = self.home / name
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return str(path)

    def sized(self, *extra, plan=None, config=None):
        return cli(["--plan", self.save("plan.json", plan if plan is not None else self.plan),
                    "--now", "2026-10-22 10:20", "--prices", self.prices,
                    "--portfolio", str(self.home / "none.json"),
                    "--config", self.save("c.json", config or {"capital": 100000}), *extra])

    # the split follows the US pack's rule exactly
    def test_with_no_minimum_a_name_that_buys_no_share_stays_at_zero_as_in_the_us_pack(self):
        """Dropping it and re-splitting gave other share counts than the US pack's (99,976
        invested against its 99,989.40 on one case); kept at 0, said, and no order."""
        prices = fixture("chain/prices.json")["prices"]
        out = budget.split({"RELIANCE": 1, "SUZLON": 1},
                           rules(budget=1500, min_order_rupees=0), prices)
        got = {s["symbol"]: s["shares"] for s in out["slices"]}
        self.assertEqual(got, {"RELIANCE": 0, "SUZLON": 30})
        self.assertEqual(out["invested"], 1122.0)
        self.assertIn("RELIANCE (one share costs Rs 1,190", out["unbought"][0])
        self.assertEqual([o["symbol"] for o in budget.weight_orders(out)], ["SUZLON"])
        # with a minimum, the 0-rupee slice is under it: dropped and re-split, as there
        out = budget.split({"RELIANCE": 1, "SUZLON": 1}, rules(budget=1500), prices)
        self.assertEqual([s["symbol"] for s in out["slices"]], ["SUZLON"])

    def test_an_order_says_its_weight_to_two_places_as_the_us_pack_does(self):
        prices = fixture("chain/prices.json")["prices"]
        orders = budget.weight_orders(budget.split({"SBIN": 40, "ITC": 60}, rules(), prices))
        self.assertTrue(orders[0]["why"].startswith("60.00% of the split"), orders[0]["why"])

    def test_a_split_price_off_the_tick_is_refused(self):
        real = fixture("chain/prices.json")["prices"]
        real["ITC"] = 257.913
        with self.assertRaises(budget.Refused) as caught:
            budget.split({"SBIN": 40, "ITC": 60}, rules(), real)
        self.assertIn("not on an NSE tick", str(caught.exception))

    def test_a_whole_share_split_that_buys_nothing_is_still_written(self):
        """As the US pack writes it: an empty proposals file, not a refusal."""
        out = str(self.home / "orders.json")
        code, said = cli(["--weights", "RELIANCE=1", "--prices", self.prices, "--config",
                          self.save("c.json", {"capital": 1000, "min_order_rupees": 0}),
                          "--write", out])
        self.assertEqual(code, 0, said)
        self.assertEqual(json.loads(Path(out).read_text(encoding="utf-8"))["orders"], [])
        self.assertIn("no slice buys one whole share", said)
        self.assertIn("Next: /nse-risk checks every order", said)

    # the report, as the US pack prints it
    def test_a_size_refusal_still_prints_the_plan_and_every_limit(self):
        code, said = self.sized(config={"capital": 100000, "max_loss_per_trade_rupees": 5})
        self.assertEqual(code, 2)
        self.assertTrue(said.startswith("SBIN buy, opening-range breakout, entry Rs 961.85"), said)
        self.assertIn("Limits (whole shares; the smallest one wins):", said)
        self.assertIn("Refused, and no order written: one share risks Rs 11.80", said)

    def test_the_report_says_the_risk_sum_and_the_stop_it_can_fill_below(self):
        code, said = self.sized()
        self.assertEqual(code, 0, said)
        self.assertIn("Risk Rs 11.80 a share (entry Rs 961.85 minus stop Rs 950.05)", said)
        self.assertIn("below Rs 950.05", said)
        self.assertIn("then /nse-broker asks you yes or no", said)

    def test_json_carries_the_sizing_fields_the_us_pack_does(self):
        code, said = self.sized("--json")
        out = json.loads(said)
        self.assertEqual((out["budget"], out["cash_buffer"], out["cash_available"],
                          out["max_loss_per_trade_rupees"], out["entry_from"], out["date"]),
                         (100000.0, 10000.0, 90000.0, 1000.0, "plan", "2026-10-22"))
        self.assertEqual(out["orders"], [out["order"]])
        code, said = self.sized("--json", config={"capital": 100000,
                                                  "max_loss_per_trade_rupees": 5})
        refusal = json.loads(said)
        self.assertEqual((refusal["orders"], refusal["sizing"]["quantity"]), ([], 0))

    def test_a_plan_with_no_flat_by_takes_the_configs_or_15_20(self):
        plan = dict(self.plan)
        plan.pop("flat_by")
        self.assertEqual(budget.size(plan, rules(), at=AT)["flat_by"], "15:20")
        self.assertEqual(budget.size(plan, rules(flat_by="15:10"), at=AT)["flat_by"], "15:10")

    # refusals that were tracebacks, text under --json, or named the wrong key
    def test_a_bad_clock_or_config_under_json_is_json(self):
        code, said = self.sized("--json", "--now", "bad")
        self.assertEqual(code, 2)
        self.assertIn("--now wants a time", json.loads(said)["refused"])
        code, said = cli(["--json", "--config", self.save("bad.json", "{ broken")])
        self.assertIn("not valid JSON", json.loads(said)["refused"])
        code, said = self.sized("--json", "--budget", "0")
        self.assertIn("more than zero", json.loads(said)["refused"])

    def test_a_capital_that_is_not_a_number_is_named_as_capital(self):
        code, said = self.sized(config={"capital": "abc"})
        self.assertEqual(code, 2)
        self.assertIn("capital in your config has to be a number, not 'abc'", said)

    def test_a_negative_risk_percent_is_refused(self):
        with self.assertRaises(budget.Refused) as caught:
            budget.size(self.plan, rules(risk_per_trade_percent=-1), at=AT)
        self.assertIn("risk_per_trade_percent", str(caught.exception))

    def test_a_figure_too_large_to_be_real_is_refused_not_a_crash(self):
        """1e308 crashed the share count with an OverflowError (in the US pack too)."""
        for over in ({"budget": 1e308}, {"max_order_rupees": 1e308},
                     {"max_loss_per_trade_rupees": 1e308}):
            with self.assertRaises(budget.Refused):
                budget.size(self.plan, rules(**over), at=AT)
        with self.assertRaises(budget.Refused):
            budget.split({"SBIN": 40, "ITC": 60}, rules(budget=1e308))
        with self.assertRaises(budget.Refused):
            budget.weights_from([("SBIN", 1e308)], "--weights")
        code, said = self.sized("--budget", "1e308")
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", said)


if __name__ == "__main__":
    unittest.main(verbosity=2)
