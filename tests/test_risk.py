"""The gate: sizing arithmetic, the order check, and the limits that belong to the day.

The arithmetic is exact and needs no data. The order check runs on the real order a run of the
chain wrote for SBIN on 2026-10-22, loaded from tests/fixtures/chain/orders.json.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load import fixture, has, skill                            # noqa: E402


def _load(folder: str, module: str):
    spec = importlib.util.spec_from_file_location(module, ROOT / folder / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _order():
    """The order a real run of nse-budget wrote."""
    if not has("chain/orders.json"):
        raise unittest.SkipTest("run capture.py")
    return fixture("chain/orders.json")["orders"][0]


risk = _load("nse-risk", "risk")


def _example():
    """The pack's own config.example.json, as a user who copied it to config.json has it."""
    import json
    doc = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    return {k: v for k, v in doc.items() if not k.startswith("_")}


EXAMPLE = _example()
_check = risk.check


def check(orders, conf=None, *args, **kw):
    """The gate with the example config under whatever the test changes: the gate refuses to run
    with no capital and no portfolio, so every test states the account it checks against."""
    return _check(orders, dict(EXAMPLE, **(conf or {})), *args, **kw)


CONF = {"capital": 500000.0, "risk_per_trade_percent": 1.0, "max_positions": 5}


class Sizing(unittest.TestCase):
    def test_shares_come_from_the_risk_budget_not_the_capital(self):
        # 1% of 5,00,000 is 5,000. A 37.40 stop distance allows 133 shares.
        out = risk.size(687.40, 650.0, CONF)
        self.assertEqual(out["verdict"], "PASS")
        self.assertEqual(out["shares"], 133)
        self.assertAlmostEqual(out["risk_budget"], 5000.0)

    def test_the_loss_at_the_stop_includes_the_cost_of_getting_out(self):
        """Costs are part of the loss, not a footnote.

        A tool that reports the loss as shares times the stop distance understates it by the whole
        Indian cost stack, which on a tight stop is a material share of the risk budget.
        """
        out = risk.size(687.40, 650.0, CONF)
        bare = out["shares"] * out["risk_per_share"]
        self.assertGreater(out["loss_if_stopped"], bare)
        self.assertAlmostEqual(out["loss_if_stopped"] - bare, out["costs_round_trip"], places=2)

    def test_a_stop_above_the_entry_is_refused(self):
        out = risk.size(100.0, 110.0, CONF)
        self.assertEqual(out["verdict"], "BLOCK")
        self.assertEqual(out["shares"], 0)
        self.assertTrue(any("above entry" in r for r in out["reasons"]))

    def test_a_stop_inside_the_noise_is_refused(self):
        """A 0.35% stop implies an enormous position and will be taken out by nothing at all."""
        out = risk.size(687.40, 685.0, CONF)
        self.assertEqual(out["verdict"], "BLOCK")
        self.assertTrue(any("closer than" in r for r in out["reasons"]))

    def test_a_stop_far_away_is_refused(self):
        out = risk.size(100.0, 60.0, CONF)
        self.assertEqual(out["verdict"], "BLOCK")
        self.assertTrue(any("beyond" in r for r in out["reasons"]))

    def test_the_position_limit_blocks_a_new_trade(self):
        out = risk.size(100.0, 90.0, CONF, open_positions=5)
        self.assertEqual(out["verdict"], "BLOCK")
        self.assertTrue(any("already open" in r for r in out["reasons"]))

    def test_one_position_short_of_the_limit_still_passes(self):
        out = risk.size(100.0, 90.0, CONF, open_positions=4)
        self.assertEqual(out["verdict"], "PASS")

    def test_a_share_too_expensive_for_the_budget_is_refused(self):
        small = {"capital": 10000.0, "risk_per_trade_percent": 1.0, "max_positions": 5}
        out = risk.size(90000.0, 80000.0, small)      # one share risks 10,000; budget is 100
        self.assertEqual(out["verdict"], "BLOCK")
        self.assertTrue(any("smallest position" in r for r in out["reasons"]))

    def test_a_position_bigger_than_capital_is_cut_to_fit(self):
        # a 0.1% stop would allow far more than the capital buys, so the size is capped
        wide = {"capital": 50000.0, "risk_per_trade_percent": 5.0, "max_positions": 5}
        out = risk.size(100.0, 98.0, wide)
        self.assertEqual(out["verdict"], "PASS")
        self.assertLessEqual(out["position_value"], wide["capital"])
        self.assertTrue(any("cut to fit capital" in n for n in out["notes"]))

    def test_reward_to_risk_is_after_costs(self):
        out = risk.size(687.40, 650.0, CONF, target=780.0)
        self.assertIn("reward_to_risk", out)
        gross = (780.0 - 687.40) / (687.40 - 650.0)
        self.assertLess(out["reward_to_risk"], gross)      # costs take a bite out of it

    def test_a_thin_reward_is_called_out(self):
        out = risk.size(100.0, 90.0, CONF, target=108.0)   # 0.8 to 1 before costs
        self.assertTrue(any("reward to risk" in n for n in out["notes"]))

    def test_costs_eating_the_budget_is_called_out(self):
        """Costs are about 0.3% of the position each way, so they matter most on a tight stop.

        The round trip is about 0.293% of the position, and the risk budget is shares times the
        stop distance, so costs against budget is roughly 0.00293 x entry / distance -- it depends
        only on how tight the stop is. A 1.5% stop spends about 19% of the budget on charges before
        the trade has done anything.

        This test is why the threshold is 15% and not a third: a third needs a stop inside 0.89%,
        and anything under 1% is blocked, so the check as first written could never fire.
        """
        out = risk.size(100.0, 98.5, CONF)            # 1.5% away: allowed, but costly
        self.assertEqual(out["verdict"], "PASS")
        self.assertGreater(out["costs_round_trip"], out["risk_budget"] * 0.15)
        self.assertTrue(any("costs are" in n for n in out["notes"]))

    def test_a_comfortable_stop_draws_no_cost_warning(self):
        out = risk.size(687.40, 650.0, CONF)          # 5.4% away
        self.assertEqual(out["verdict"], "PASS")
        self.assertFalse(any("costs are" in n for n in out["notes"]))


class Costs(unittest.TestCase):
    def test_stamp_duty_is_charged_on_the_buy_only(self):
        buy = risk.costs(100000.0, buying=True)
        sell = risk.costs(100000.0, buying=False)
        self.assertGreater(buy["stamp"], 0)
        self.assertEqual(sell["stamp"], 0.0)
        self.assertGreater(buy["total"], sell["total"])

    def test_gst_falls_on_brokerage_exchange_charges_and_the_sebi_fee(self):
        c = risk.costs(100000.0, buying=True)
        expected = (c["brokerage"] + c["exchange"] + c["sebi"]) * 0.18
        self.assertAlmostEqual(c["gst"], expected, places=6)

    def test_every_component_is_present_and_adds_up(self):
        c = risk.costs(250000.0, buying=True)
        parts = ("brokerage", "stt", "exchange", "sebi", "stamp", "gst")
        for part in parts:
            self.assertIn(part, c)
        self.assertAlmostEqual(c["total"], sum(c[p] for p in parts), places=6)


class Defaults(unittest.TestCase):
    def test_it_runs_with_no_config_at_all(self):
        """No --config and no config.json anywhere: the defaults, and the gate still works."""
        import os
        import tempfile
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                conf = risk.settings(None)
            finally:
                os.chdir(here)
        self.assertEqual(conf["capital"], 100000.0)
        self.assertEqual(conf["risk_per_trade_percent"], 1.0)
        self.assertNotIn("_from", conf)

    def test_a_config_named_explicitly_that_does_not_exist_is_refused(self):
        """A typo in --config must not silently fall back to the default limits.

        For a risk gate that is the dangerous failure: the user believes their stricter limits are
        in force and they are not, and nothing on screen says so.
        """
        with self.assertRaises(FileNotFoundError):
            risk.settings(Path("/definitely/not/here.json"))

    def test_a_config_overrides_the_defaults(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"capital": 750000, "risk_per_trade_percent": 2.5}))
            conf = risk.settings(path)
            self.assertEqual(conf["capital"], 750000.0)
            self.assertEqual(conf["risk_per_trade_percent"], 2.5)
            self.assertEqual(conf["max_positions"], 5)      # untouched keys keep the default

    def test_a_broken_config_is_refused_not_replaced_by_the_defaults(self):
        """As the US pack refuses it: quietly sizing against the defaults would drop your limits."""
        import tempfile
        from nsetools import settings as rules_file
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json at all")
            with self.assertRaises(rules_file.BadConfig):
                risk.settings(path)


class TheReferenceExample(unittest.TestCase):
    """The US pack documents its own gate on one trade. A faithful port must give its numbers.

    TSLA at 10:15 on 2026-10-19: entry 364.18, stop 358.32, target 370.04, on a fresh 5,000 paper
    account with the US example config. Currency aside, the arithmetic is the same arithmetic.
    """

    CFG = {"capital": 5000, "cash_buffer_pct": 10, "max_position_pct": 75,
           "max_order_rupees": 4000, "min_order_rupees": 5, "max_loss_per_trade_rupees": 50,
           "max_daily_loss_rupees": 150, "max_trades_per_day": 3, "max_orders_per_day": 10,
           "min_reward_risk": 1.0, "require_stop": True,
           "no_entries_before": "09:45", "no_entries_after": "15:30"}

    @staticmethod
    def order(quantity):
        return {"symbol": "TSLA", "exchange": "NSE", "side": "buy", "quantity": quantity,
                "order_type": "MARKET", "product": "MIS", "validity": "DAY",
                "order_class": "bracket", "stop_loss_price": 358.32, "take_profit_price": 370.04,
                "plan": {"entry": 364.18, "stop": 358.32, "target": 370.04}}

    def at(self, hhmm):
        from nsetools import clock
        return clock.now(f"2026-10-19 {hhmm}")

    def one(self, orders, **kw):
        res = check(orders, self.CFG, at=kw.pop("at", self.at("10:15")), offline=True, **kw)
        return res, res["results"][0]

    @staticmethod
    def not_tick(row):
        """The US prices are in cents, which the NSE refuses above Rs 250 (a 5-paise tick). That
        one Indian refusal aside, every reason must be the reference's."""
        return [r for r in row["reasons"] if not r.startswith("tick:")]

    def test_eight_shares_pass_with_its_figures(self):
        res, row = self.one([self.order(8)])
        self.assertEqual(self.not_tick(row), [])
        self.assertTrue(all("358.32" in r or "370.04" in r
                            for r in row["reasons"] if r.startswith("tick:")))
        self.assertEqual(row["max_loss"], 46.88)
        self.assertEqual(row["reward_risk"], 1.0)
        self.assertEqual(round(5000 - row["value_rupees"], 2), 2086.56)

    def test_one_more_share_is_blocked_on_the_loss(self):
        _, row = self.one([self.order(9)])
        self.assertEqual(row["verdict"], "BLOCK")
        self.assertEqual(row["max_loss"], 52.74)
        self.assertTrue(any(r.startswith("loss:") for r in row["reasons"]))

    def test_a_fresh_higher_price_blocks_it_twice(self):
        """The 10:52 quote of 366.48: 8 x 8.16 = 65.28 at the stop, and reward:risk 0.44."""
        _, row = self.one([self.order(8)], at=self.at("10:55"), prices={"TSLA": 366.48},
                          price_times={"TSLA": "2026-10-19T10:52:00+05:30"})
        self.assertEqual(row["max_loss"], 65.28)
        self.assertEqual(row["reward_risk"], 0.44)
        self.assertTrue(any(r.startswith("loss:") for r in row["reasons"]))
        self.assertTrue(any(r.startswith("reward:risk:") for r in row["reasons"]))

    def test_before_the_window_opens_it_is_blocked(self):
        _, row = self.one([self.order(8)], at=self.at("09:40"))
        self.assertEqual(row["verdict"], "BLOCK")
        self.assertTrue(self.not_tick(row))
        self.assertTrue(any("no new entries before 09:45" in r for r in row["reasons"]))

    def test_the_closing_sell_after_the_cutoff_passes_because_it_is_an_exit(self):
        sell = {"symbol": "TSLA", "exchange": "NSE", "side": "sell", "quantity": 8,
                "order_type": "MARKET", "product": "MIS", "validity": "DAY"}
        _, row = self.one([sell], at=self.at("15:55"),
                          portfolio={"cash": 2086.56, "positions": [{"symbol": "TSLA", "shares": 8}]},
                          prices={"TSLA": 366.0}, price_times={"TSLA": "2026-10-19T15:54:00+05:30"})
        self.assertEqual(row["kind"], "exit")
        self.assertEqual(row["verdict"], "PASS")


class EveryCheckOnTheRealOrder(unittest.TestCase):
    """Each of the gate's checks, on the order a real run of the chain wrote for SBIN."""

    def setUp(self):
        from nsetools import clock
        self.base = dict(_order())
        self.at = clock.now("2026-10-22 11:00")

    def verdicts(self, orders, conf=None, **kw):
        return [(r["verdict"], r["reasons"], r["warnings"])
                for r in check(orders, conf or {}, at=self.at, offline=True, **kw)["results"]]

    def reason(self, orders, word, conf=None, **kw):
        found = self.verdicts(orders, conf, **kw)
        return any(any(r.startswith(word) for r in reasons) for _, reasons, _ in found)

    def test_the_real_order_passes(self):
        self.assertEqual(self.verdicts([self.base])[0][0], "PASS")

    def test_a_stop_exactly_at_the_floor_is_not_closer_than_it(self):
        """965.00 to 955.35 is exactly 1%; as floats it was 0.9999999999999976% and was blocked.
        The real SBIN order, its entry moved to a LIMIT at 965.00 and its stop to the floor."""
        at_floor = dict(self.base, order_type="LIMIT", price=965.00, stop_loss_price=955.35,
                        take_profit_price=985.00, quantity=10,
                        plan=dict(self.base["plan"], entry=965.00, stop=955.35, target=985.00))
        self.assertFalse(self.reason([at_floor], "stop:"), self.verdicts([at_floor]))
        inside = dict(at_floor, stop_loss_price=955.40, plan=dict(at_floor["plan"], stop=955.40))
        self.assertTrue(self.reason([inside], "stop:"))
        sized = risk.size(965.00, 955.35, dict(EXAMPLE))
        self.assertFalse(any("closer than" in b for b in sized.get("blocks", [])), sized)

    def test_format(self):
        for broken, word in ((dict(self.base, symbol=""), "format: no symbol"),
                             (dict(self.base, side="hold"), "format: side"),
                             (dict(self.base, quantity=0), "format: quantity"),
                             (dict(self.base, quantity=4.5), "format: Indian equities"),
                             (dict(self.base, order_type="STOP"), "format: unknown order type"),
                             (dict(self.base, order_type="LIMIT", price=None), "format: a LIMIT"),
                             (dict(self.base, order_type="SL-M"), "format: a SL-M"),
                             (dict(self.base, product="XYZ"), "format: unknown product")):
            self.assertTrue(self.reason([broken], word), word)

    def test_asset_a_derivative_is_blocked_unless_allowed(self):
        for contract in ("NIFTY26OCT25000CE", "BANKNIFTY26NOV52000PE", "RELIANCE26DECFUT"):
            self.assertTrue(self.reason([dict(self.base, symbol=contract)], "asset:"), contract)
        self.assertFalse(self.reason([dict(self.base, symbol="NIFTY26OCT25000CE", exchange="NFO")],
                                     "asset:", {"allow_options": True}))

    def test_real_nse_symbols_are_not_mistaken_for_derivatives(self):
        for symbol in ("SBIN", "M&M", "BAJAJ-AUTO", "3PLAND", "NIFTYBEES"):
            self.assertFalse(self.reason([dict(self.base, symbol=symbol)], "asset:"), symbol)

    def test_blocked(self):
        self.assertTrue(self.reason([self.base], "blocked:", {"blocked": ["sbin"]}))

    def test_size(self):
        self.assertTrue(self.reason([dict(self.base, quantity=200)], "size:",
                                    {"max_loss_per_trade_rupees": 10 ** 9}))
        self.assertTrue(self.reason([dict(self.base, quantity=1)], "size:",
                                    {"min_order_rupees": 5000}))

    def test_position_cap(self):
        self.assertTrue(self.reason([self.base], "position:", {"max_position_pct": 30}))

    def test_cash_buffer_and_margin(self):
        self.assertTrue(self.reason([self.base], "cash: leaves", {"capital": 42000}))
        self.assertTrue(self.reason([self.base], "cash: needs", {"capital": 30000}))

    def test_stop_missing_and_on_the_wrong_side(self):
        bare = {k: v for k, v in self.base.items()
                if k not in ("stop_loss_price", "take_profit_price", "order_class")}
        bare["plan"] = {"entry": 961.85}
        self.assertTrue(self.reason([bare], "stop: no stop"))
        wrong = dict(self.base, stop_loss_price=970.0, plan=dict(self.base["plan"], stop=970.0))
        self.assertTrue(self.reason([wrong], "stop: the stop"))

    def test_the_indian_noise_floor(self):
        """0.75% from the entry sits inside one minute's range on an Indian mid cap."""
        tight = dict(self.base, stop_loss_price=954.65, plan=dict(self.base["plan"], stop=954.65))
        self.assertTrue(self.reason([tight], "stop: 0.75%"))
        self.assertFalse(self.reason([tight], "stop:", {"min_stop_percent": 0.5}))

    def test_reward_risk_and_target(self):
        poor = dict(self.base, take_profit_price=962.5, plan=dict(self.base["plan"], target=962.5))
        self.assertTrue(self.reason([poor], "reward:risk:"))
        under = dict(self.base, take_profit_price=900.0, plan=dict(self.base["plan"], target=900.0))
        self.assertTrue(self.reason([under], "target:"))

    def test_window_and_holiday(self):
        from nsetools import clock
        late = check([self.base], {}, at=clock.now("2026-10-22 15:10"), offline=True)
        self.assertTrue(any("no new entries after" in r for r in late["results"][0]["reasons"]))
        shut = check([self.base], {}, at=clock.now("2026-10-20 11:00"), offline=True)
        self.assertTrue(any("Dussehra" in r for r in shut["results"][0]["reasons"]))

    def test_duplicate_in_a_batch(self):
        found = self.verdicts([self.base, dict(self.base)], {"max_position_pct": 100})
        self.assertEqual(found[0][0], "PASS")
        self.assertTrue(any(r.startswith("duplicate:") for r in found[1][1]))

    def test_orders_are_checked_in_order_as_if_each_pass_had_filled(self):
        """Fine one by one, too many together: the fourth breaks the three-a-day limit."""
        big = {"max_order_rupees": 10 ** 9, "max_position_pct": 100, "cash_buffer_pct": 0,
               "capital": 10 ** 7, "max_daily_loss_rupees": 10 ** 9}
        found = self.verdicts([dict(self.base, symbol=s) for s in ("SBIN", "ITC", "INFY", "TCS")],
                              big)
        self.assertEqual([v for v, _, _ in found], ["PASS", "PASS", "PASS", "BLOCK"])
        self.assertTrue(any(r.startswith("trades:") for r in found[3][1]))

    def test_the_daily_loss_counts_across_the_batch(self):
        big = {"max_order_rupees": 10 ** 9, "max_position_pct": 100, "cash_buffer_pct": 0,
               "capital": 10 ** 7, "max_daily_loss_rupees": 900, "max_trades_per_day": 10}
        found = self.verdicts([dict(self.base, symbol=s) for s in ("SBIN", "ITC")], big)
        self.assertEqual(found[0][0], "PASS")
        self.assertTrue(any(r.startswith("daily loss:") for r in found[1][1]))

    def test_the_daily_order_count(self):
        conf = {"max_orders_per_day": 1, "max_order_rupees": 10 ** 9, "capital": 10 ** 7,
                "max_position_pct": 100}
        found = self.verdicts([self.base, dict(self.base, symbol="ITC")], conf)
        self.assertTrue(any(r.startswith("daily limit:") for r in found[1][1]))

    def test_short_selling_is_blocked(self):
        sell = {k: self.base[k] for k in ("symbol", "exchange", "order_type", "product",
                                          "validity")}
        sell.update(side="sell", quantity=100)
        found = self.verdicts([sell], portfolio={"cash": 50000,
                                                 "positions": [{"symbol": "SBIN", "shares": 41}]})
        self.assertTrue(any(r.startswith("short:") for r in found[0][1]))

    def test_an_exit_turns_every_entry_rule_into_a_warning(self):
        """Getting out must always be possible, even of a stock now on your blocked list."""
        sell = {k: self.base[k] for k in ("symbol", "exchange", "order_type", "product",
                                          "validity")}
        sell.update(side="sell", quantity=41)
        found = self.verdicts([sell], {"blocked": ["SBIN"]},
                              portfolio={"cash": 50000, "positions": [{"symbol": "SBIN",
                                                                       "shares": 41}]},
                              prices={"SBIN": 958.6},
                              price_times={"SBIN": "2026-10-22T10:59:00+05:30"})
        verdict, reasons, warnings = found[0]
        self.assertEqual(verdict, "PASS")
        self.assertTrue(any("exit, not blocked: blocked:" in w for w in warnings))

    def test_a_fresh_price_above_the_plan_is_what_the_risk_is_measured_from(self):
        res = check([self.base], {}, at=self.at, offline=True, prices={"SBIN": 965.0},
                         price_times={"SBIN": "2026-10-22T10:58:00+05:30"})
        self.assertEqual(res["results"][0]["entry_ref"], 965.0)

    def test_an_old_price_is_not_used_and_says_so(self):
        res = check([self.base], {}, at=self.at, offline=True, prices={"SBIN": 990.0},
                         price_times={"SBIN": "2026-10-22T09:00:00+05:30"})
        row = res["results"][0]
        self.assertEqual(row["entry_ref"], 961.85)
        self.assertTrue(any("not from the last 15 minutes" in w for w in row["warnings"]))

    def test_it_says_the_stop_and_target_are_not_at_the_broker(self):
        """Zerodha's order tool has no bracket variety, so the exits travel only as a plan."""
        warnings = self.verdicts([self.base])[0][2]
        self.assertTrue(any("Zerodha has no bracket order" in w for w in warnings))

    def test_the_exit_note_says_how_the_broker_places_them_by_product(self):
        """MIS: entry, then an SL-M at the stop after each fill, the target watched by
        /nse-alerts and never placed. CNC and MTF: one GTT, two-leg with both, single-leg with a
        stop only. The old note said each exit is placed separately, which is not the design."""
        def note(order):
            found = [w for w in self.verdicts([order])[0][2] if "not at the broker yet" in w]
            self.assertEqual(len(found), 1, found)
            return found[0]
        said = ("MIS entry, then an SL-M at the stop after each fill; the target is never placed "
                "at Zerodha for MIS (watched by /nse-alerts); CNC/MTF get a GTT (two-leg, or "
                "single-leg when stop only)")
        for product in ("MIS", "CNC", "MTF"):
            text = note(dict(self.base, product=product))
            self.assertTrue(text.endswith(said), text)
            self.assertNotIn("placed separately", text)
        self.assertIn(said, (ROOT / "nse-risk" / "SKILL.md").read_text(encoding="utf-8")
                      .replace("\n", " "))

    def test_the_exit_note_says_when_an_order_gets_no_protection(self):
        """ticket.py protection_call places nothing for a CNC sell or a target-only order, and an
        SL-M for NRML: the note must not promise a GTT or an SL-M that never comes."""
        def note(order):
            return [w for w in self.verdicts([order], {"allow_options": True})[0][2]
                    if "not at the broker yet" in w][0]
        sell = dict(self.base, side="sell", product="CNC")
        self.assertIn("This CNC sell gets none", note(sell))
        only_target = {k: v for k, v in self.base.items() if k != "stop_loss_price"}
        for product in ("MIS", "CNC"):
            self.assertIn("only a target, so nothing is placed",
                          note(dict(only_target, product=product)))
        self.assertIn("NRML order is protected as MIS is", note(dict(self.base, product="NRML")))

    def test_a_live_account_is_flagged(self):
        res = check([self.base], {"paper": False}, at=self.at, offline=True)
        self.assertTrue(any("LIVE" in w for w in res["warnings"]))


class TheSameOrderCheckedTwiceIsOneTrade(unittest.TestCase):
    """Verdicts are kept by fingerprint, so a re-check is not a second trade."""

    def setUp(self):
        from nsetools import clock
        self.base = dict(_order())
        self.at = clock.now("2026-10-22 11:00")

    def test_a_changed_order_has_a_different_fingerprint(self):
        self.assertNotEqual(risk.fingerprint(self.base),
                            risk.fingerprint(dict(self.base, quantity=40)))

    def test_the_notes_do_not_change_the_fingerprint(self):
        self.assertEqual(risk.fingerprint(self.base),
                         risk.fingerprint(dict(self.base, why="something else")))

    def test_rechecking_an_order_that_passed_today_counts_it_once(self):
        first = check([self.base], {}, at=self.at, offline=True)
        state = {"date": "2026-10-22", "passed": [
            {"fingerprint": r["fingerprint"], "symbol": r["symbol"], "side": r["side"],
             "kind": r["kind"], "max_loss": r["max_loss"]} for r in first["results"]]}
        again = check([self.base], {}, at=self.at, offline=True, risk_state=state)
        self.assertEqual(again["results"][0]["verdict"], "PASS")
        self.assertEqual(again["today"]["entries"], 1)

    def test_a_different_order_on_the_same_stock_after_one_passed_is_a_duplicate(self):
        first = check([self.base], {}, at=self.at, offline=True)
        state = {"date": "2026-10-22", "passed": [
            {"fingerprint": r["fingerprint"], "symbol": r["symbol"], "side": r["side"],
             "kind": r["kind"], "max_loss": r["max_loss"]} for r in first["results"]]}
        changed = dict(self.base, quantity=30)
        again = check([changed], {}, at=self.at, offline=True, risk_state=state)
        self.assertTrue(any(r.startswith("duplicate:") for r in again["results"][0]["reasons"]))

    def test_a_broker_log_is_what_counts_when_there_is_one(self):
        log = {"orders": [{"status": "placed", "date": "2026-10-22", "symbol": "ITC",
                           "side": "buy", "kind": "entry", "max_loss": 100.0}]}
        res = check([self.base], {}, at=self.at, offline=True, broker_state=log)
        self.assertEqual(res["today"]["counted_from"], "the broker's log")
        self.assertEqual(res["today"]["entries"], 1)


class TheAuditFixes(unittest.TestCase):
    """Each gap found by checking this gate against the US pack, on the real SBIN order with one
    fact changed at a time."""

    def setUp(self):
        from nsetools import clock
        self.base = dict(_order())
        self.at = clock.now("2026-10-22 11:00")
        self.bare = {k: v for k, v in self.base.items()
                     if k not in ("stop_loss_price", "take_profit_price", "order_class")}

    def run_(self, orders, conf=None, **kw):
        return check(orders, conf or {}, at=self.at, offline=True, **kw)

    def row(self, order, conf=None, **kw):
        return self.run_([order], conf, **kw)["results"][0]

    def passed_state(self, res):
        return {"date": "2026-10-22", "passed": [
            {"fingerprint": r["fingerprint"], "symbol": r["symbol"], "side": r["side"],
             "kind": r["kind"], "max_loss": r["max_loss"]} for r in res["results"]
            if r["verdict"] == "PASS"]}

    def sell(self, quantity):
        out = {k: self.base[k] for k in ("symbol", "exchange", "order_type", "product", "validity")}
        out.update(side="sell", quantity=quantity)
        return out

    # 1. assets
    def test_weekly_options_and_non_equities_are_blocked(self):
        for symbol in ("NIFTY2610625000CE", "NIFTY26O2725000CE", "BANKNIFTY26N1152000PE",
                       "NIFTY26OCT24550.5PE"):
            reasons = self.row(dict(self.base, symbol=symbol))["reasons"]
            self.assertTrue(any(r.startswith("asset:") and "derivative" in r for r in reasons),
                            symbol)
        for symbol in ("BTCUSD", "USDINR", "ETHUSDT"):
            self.assertTrue(any(r.startswith("asset:")
                                for r in self.row(dict(self.base, symbol=symbol))["reasons"]),
                            symbol)

    def test_no_real_nse_symbol_is_taken_for_a_derivative_or_a_currency(self):
        """Every symbol in the saved equity list and bhavcopy passes the asset check."""
        if not (has("exchange/equities.json.gz") and has("exchange/bhavcopy.json.gz")):
            raise unittest.SkipTest("run capture.py")
        symbols = {r["symbol"] for r in fixture("exchange/equities.json.gz")}
        symbols |= {r["symbol"] for r in fixture("exchange/bhavcopy.json.gz")["rows"]}
        self.assertGreater(len(symbols), 2000)
        wrong = [s for s in symbols if risk.DERIVATIVE.search(s) or risk.NOT_EQUITY.search(s)
                 or not risk.SYMBOL.match(s)]
        self.assertEqual(wrong, [])

    def test_the_derivatives_segment_needs_allow_options(self):
        self.assertTrue(any(r.startswith("asset: NFO")
                            for r in self.row(dict(self.base, exchange="NFO"))["reasons"]))
        option = dict(self.base, symbol="NIFTY26OCT25000CE", exchange="NFO")
        self.assertFalse(any(r.startswith("asset:")
                             for r in self.row(option, {"allow_options": True})["reasons"]))
        on_cash = dict(self.base, symbol="NIFTY26OCT25000CE")
        self.assertTrue(any("trades on NFO" in r
                            for r in self.row(on_cash, {"allow_options": True})["reasons"]))

    def test_mtf_needs_allow_margin_and_nrml_needs_allow_options(self):
        self.assertTrue(any(r.startswith("product: MTF")
                            for r in self.row(dict(self.base, product="MTF"))["reasons"]))
        self.assertFalse(any(r.startswith("product:") for r in
                             self.row(dict(self.base, product="MTF"), {"allow_margin": True})["reasons"]))
        self.assertTrue(any(r.startswith("product: NRML")
                            for r in self.row(dict(self.base, product="NRML"))["reasons"]))
        self.assertEqual(self.row(dict(self.base, product="CNC"))["verdict"], "PASS")

    def test_a_price_off_the_tick_is_blocked(self):
        for field, value in (("stop_loss_price", 950.03), ("take_profit_price", 985.47)):
            changed = dict(self.base, **{field: value})
            self.assertTrue(any(r.startswith(f"tick: {field}")
                                for r in self.row(changed)["reasons"]), field)
        limit = dict(self.base, order_type="LIMIT", price=961.83)
        self.assertTrue(any(r.startswith("tick: price") for r in self.row(limit)["reasons"]))
        self.assertFalse(any(r.startswith("tick:") for r in self.row(
            dict(limit, price=961.85))["reasons"]))
        # Below Rs 250 the tick is one paisa: 247.93 is a good price there.
        cheap = dict(self.base, symbol="ITC", quantity=100, stop_loss_price=240.01,
                     take_profit_price=270.01, plan={"entry": 247.93, "stop": 240.01,
                                                     "target": 270.01})
        self.assertFalse(any(r.startswith("tick:") for r in self.row(cheap)["reasons"]))

    # 2. shares move whenever the quantity is known
    def test_two_unpriced_sells_cannot_add_up_to_a_short(self):
        held = {"cash": 50000, "positions": [{"symbol": "SBIN", "shares": 15, "cost": 14000}]}
        res = self.run_([self.sell(10), self.sell(10)], portfolio=held)
        first, second = res["results"]
        self.assertEqual((first["kind"], first["verdict"]), ("exit", "PASS"))
        self.assertEqual((second["kind"], second["verdict"]), ("entry", "BLOCK"))
        self.assertTrue(any(r.startswith("short:") and "(5 shares)" in r
                            for r in second["reasons"]))

    # 3. a re-check is not a second trade
    def test_rechecking_does_not_count_against_its_own_trade_or_loss_limit(self):
        for conf in ({"max_trades_per_day": 1}, {"max_daily_loss_rupees": 600}):
            first = self.run_([self.base], conf)
            self.assertEqual(first["results"][0]["verdict"], "PASS")
            again = self.run_([self.base], conf, risk_state=self.passed_state(first))
            self.assertEqual(again["results"][0]["verdict"], "PASS", conf)
        # A different order still counts the first one.
        first = self.run_([self.base], {"max_trades_per_day": 1})
        other = dict(self.base, symbol="ITC", stop_loss_price=950.0)
        again = self.run_([other], {"max_trades_per_day": 1},
                          risk_state=self.passed_state(first))
        self.assertTrue(any(r.startswith("trades:") for r in again["results"][0]["reasons"]))

    # 4. config numbers written as text
    def test_limits_written_as_text_are_read_as_numbers(self):
        poor = {"cash": 40000, "positions": []}
        row = self.row(self.base, {"cash_buffer_pct": "10", "capital": "40000"}, portfolio=poor)
        self.assertTrue(any(r.startswith("cash: leaves") and "10% buffer" in r
                            for r in row["reasons"]))
        row = self.row(self.base, {"max_position_pct": "30"})
        self.assertTrue(any(r.startswith("position:") and "30% cap" in r for r in row["reasons"]))
        with self.assertRaises(risk.GateError):
            self.row(self.base, {"max_position_pct": "lots"})

    # 5. the three warnings
    def test_a_stop_only_in_the_plan_is_said(self):
        warnings = self.row(self.bare)["warnings"]
        self.assertTrue(any("in the plan only, not at the broker" in w for w in warnings))
        self.assertFalse(any("in the plan only" in w for w in self.row(self.base)["warnings"]))

    def test_an_entry_logged_without_a_max_loss_is_said(self):
        log = {"orders": [{"status": "placed", "date": "2026-10-22", "symbol": "ITC",
                           "side": "buy", "kind": "entry", "max_loss": None}]}
        res = self.run_([self.base], broker_state=log)
        self.assertTrue(any("has no max loss recorded" in w for w in res["warnings"]))

    def test_an_order_class_other_than_bracket_is_said(self):
        row = self.row(dict(self.bare, order_class="oco"))
        self.assertTrue(any(w.startswith("advanced order (oco)") for w in row["warnings"]))

    # 6. what it prints
    def test_it_prints_the_risk_a_share_the_limits_and_what_today_has_used(self):
        log = {"orders": [{"status": "placed", "date": "2026-10-22", "symbol": "ITC",
                           "side": "buy", "kind": "entry", "max_loss": 300.0}]}
        info = self.row(self.base, broker_state=log)["info"]
        self.assertEqual(info[0], "entry 961.85 (the plan's entry), stop 950.05, target 985.45: "
                                  "risk 11.80 a share")
        self.assertTrue(info[1].startswith("max loss Rs 483.80 (limit Rs 1,000.00) at the stop"))
        self.assertTrue(info[1].endswith("reward:risk 2.00 (min 1)"))
        self.assertEqual(info[2], "today before this: 1 of 3 trades, Rs 300.00 of Rs 3,000.00 "
                                  "daily loss")

    # 7. the smaller ones
    def test_a_logged_sell_without_a_kind_is_not_an_entry(self):
        log = {"orders": [{"status": "placed", "date": "2026-10-22", "symbol": s, "side": "sell"}
                          for s in ("ITC", "INFY", "TCS")]}
        res = self.run_([self.base], broker_state=log)
        self.assertEqual(res["today"]["entries"], 0)
        self.assertEqual(res["results"][0]["verdict"], "PASS")

    def test_min_stop_percent_zero_switches_the_floor_off(self):
        tight = dict(self.base, stop_loss_price=958.0, plan=dict(self.base["plan"], stop=958.0))
        self.assertTrue(any(r.startswith("stop:") for r in self.row(tight)["reasons"]))
        self.assertFalse(any(r.startswith("stop:")
                             for r in self.row(tight, {"min_stop_percent": 0})["reasons"]))

    def test_counted_from_names_what_was_counted(self):
        self.assertEqual(self.run_([self.base], broker_state={"note": "no orders list"})
                         ["today"]["counted_from"], "this gate's passes")

    def test_the_gate_needs_no_network_by_default(self):
        """check() defaults to the built-in holiday list; the test harness refuses any fetch."""
        from nsetools import clock
        res = check([self.base], {}, at=self.at)
        self.assertEqual(res["results"][0]["verdict"], "PASS")
        later = check([self.base], {}, at=clock.now("2027-01-05 11:00"))
        self.assertTrue(any("--online" in r for r in later["results"][0]["reasons"]))


class TheCommandLine(unittest.TestCase):
    """The gate run as a command, in an empty folder with the real orders file."""

    def run_cli(self, *args):
        import contextlib
        import io
        import json
        import os
        import tempfile
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path("nse").mkdir()
                Path("nse/orders.json").write_text(json.dumps({"orders": [_order()]}))
                Path("config.json").write_text(json.dumps(EXAMPLE))
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = risk.main(list(args))
                files = sorted(p.name for p in Path("nse/state").glob("*")) \
                    if Path("nse/state").exists() else []
            finally:
                os.chdir(here)
        return code, out.getvalue(), err.getvalue(), files

    def test_a_date_that_does_not_exist_is_a_plain_refusal(self):
        code, _, err, _ = self.run_cli("--now", "2026-10-22 11:00", "--today", "2026-02-30",
                                       "--no-write")
        self.assertEqual(code, 1)
        self.assertIn("not a real date", err)

    def test_the_verdicts_are_written_whole_and_no_temp_file_is_left(self):
        code, out, _, files = self.run_cli("--now", "2026-10-22 11:00")
        self.assertEqual(code, 0, out)
        self.assertEqual(files, ["risk.json"])


def _prices():
    """The prices file a real run of the chain wrote: an as_of, and no quote time per price."""
    if not has("chain/prices.json"):
        raise unittest.SkipTest("run capture.py")
    return fixture("chain/prices.json")


class TheSecondAudit(unittest.TestCase):
    """Gaps found by a second side-by-side check against the US pack, each on the real SBIN order
    (and the real prices file) with one fact changed."""

    def setUp(self):
        from nsetools import clock
        self.base = dict(_order())
        self.at = clock.now("2026-10-22 11:00")

    def row(self, order, conf=None, **kw):
        return check([order], conf or {}, at=self.at, offline=True, **kw)["results"][0]

    def sell(self, quantity):
        out = {k: self.base[k] for k in ("symbol", "exchange", "order_type", "product", "validity")}
        out.update(side="sell", quantity=quantity)
        return out

    def cli(self, files, *args):
        """risk.main in an empty folder holding these files; (exit code, stdout, stderr)."""
        import contextlib
        import io
        import json
        import os
        import tempfile
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path("nse/state").mkdir(parents=True)
                files = dict({"config.json": EXAMPLE}, **files)
                for name, content in files.items():
                    Path(name).write_text(content if isinstance(content, str)
                                          else json.dumps(content), encoding="utf-8")
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = risk.main(list(args))
            finally:
                os.chdir(here)
        return code, out.getvalue(), err.getvalue()

    # the entry it measures from
    def test_a_fresh_price_is_taken_to_the_paisa_half_up(self):
        """As the US pack takes a quote to the cent: 961.855 is paid as 961.86."""
        row = self.row(self.base, prices={"SBIN": 961.855},
                       price_times={"SBIN": "2026-10-22T10:58:00+05:30"})
        self.assertEqual(row["entry_ref"], 961.86)
        self.assertEqual(row["value_rupees"], round(41 * 961.86, 2))
        self.assertEqual(row["risk_per_share"], 11.81)

    def test_the_real_prices_file_has_no_quote_times_so_nothing_in_it_is_fresh(self):
        """Its as_of is when the file was written. Judged by that, a day-old price looks fresh."""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            path.write_text(json.dumps(_prices()), encoding="utf-8")
            prices, times = risk.load_prices(path)
        self.assertEqual(prices["SBIN"], 958.6)
        self.assertIsNone(times["SBIN"])

    def test_a_damaged_prices_file_is_refused_not_read_as_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(risk.GateError):
                risk.load_prices(path)
            path.write_text("[958.6]", encoding="utf-8")
            with self.assertRaises(risk.GateError):
                risk.load_prices(path)

    def test_a_plain_symbol_to_price_file_is_read(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            path.write_text(json.dumps(_prices()["prices"]), encoding="utf-8")
            prices, _ = risk.load_prices(path)
        self.assertEqual(prices["ITC"], 257.9)

    def test_an_old_price_used_for_want_of_any_other_is_said(self):
        bare_plan = dict(self.base, plan={k: v for k, v in self.base["plan"].items()
                                          if k != "entry"})
        row = self.row(bare_plan, prices={"SBIN": 958.6}, price_times={"SBIN": None})
        self.assertEqual(row["entry_ref_from"], "an old price")
        self.assertTrue(any("only SBIN price is not a fresh one" in w for w in row["warnings"]))

    # --today
    def test_today_moves_the_day_the_limits_count_from_but_not_the_clock(self):
        """The US pack's --today sets only the day. Moving the clock with it judged the window on
        another day and a fresh price as hours old."""
        doc = _prices()
        doc["detail"] = {"SBIN": {"at": "2026-10-22T10:58:00+05:30"}}
        doc["prices"]["SBIN"] = 965.0
        files = {"nse/orders.json": {"orders": [self.base]}, "nse/prices.json": doc}
        code, out, err = self.cli(files, "--now", "2026-10-22 11:00", "--today", "2026-10-20",
                                  "--json", "--no-write")
        import json
        res = json.loads(out)
        self.assertEqual((code, res["date"], res["now_ist"]), (0, "2026-10-20", "2026-10-22 11:00"),
                         err)
        self.assertEqual(res["results"][0]["entry_ref"], 965.0)        # still fresh
        self.assertEqual(res["prices_as_of"], doc["as_of"])

    # the account
    def test_a_position_kept_as_lots_is_held(self):
        held = {"cash": 50000, "positions": [{"ticker": "SBIN",
                                              "lots": [{"shares": 41, "cost": 39000.0}]}]}
        res = check([self.sell(41)], {}, held, at=self.at, offline=True)
        self.assertEqual(res["results"][0]["kind"], "exit")
        self.assertTrue(any("Rs 39,000.00" in w for w in res["warnings"]))

    def test_a_share_count_that_cannot_be_read_is_refused_not_taken_as_none(self):
        for held in ({"cash": 50000, "positions": [{"symbol": "SBIN", "shares": "forty"}]},
                     {"cash": 50000, "positions": {"SBIN": 41}},
                     {"cash": 50000, "positions": ["SBIN"]}, [50000]):
            with self.assertRaises(risk.GateError, msg=repr(held)):
                check([self.sell(41)], {}, held, at=self.at, offline=True)

    def test_an_order_that_is_not_an_object_is_refused(self):
        with self.assertRaises(risk.GateError):
            check([self.base, "SBIN"], {}, at=self.at, offline=True)

    # what it prints
    def test_a_fractional_quantity_is_shown_as_given(self):
        res = check([dict(self.base, quantity=4.5)], {}, at=self.at, offline=True)
        self.assertEqual(res["results"][0]["quantity"], 4.5)
        self.assertIn("BLOCK #1 buy SBIN 4.5 shares", risk.report(res))

    def test_no_loss_figure_is_made_up_for_an_unreadable_quantity(self):
        row = self.row(dict(self.base, quantity="nan"))
        self.assertIsNone(row["max_loss"])
        self.assertFalse(any(x.startswith("max loss") for x in row["info"]))

    def test_a_short_says_how_to_record_a_fill(self):
        row = self.row(self.sell(10))
        self.assertTrue(any(r.startswith("short:") and "ticket.py fill" in r
                            for r in row["reasons"]))

    # the config
    def test_blocked_written_as_one_symbol_still_blocks_it(self):
        """Read as a list, "SBIN" is S, B, I, N, and SBIN itself passed."""
        self.assertTrue(any(r.startswith("blocked:")
                            for r in self.row(self.base, {"blocked": "sbin"})["reasons"]))

    def test_a_switch_written_as_text_is_refused(self):
        for key in ("allow_options", "allow_short", "paper", "require_stop"):
            with self.assertRaises(risk.GateError, msg=key):
                self.row(self.base, {key: "false"})
        # Live or paper: null and 0 are refused as /nse-broker refuses them, never read as live.
        for value in (None, 0):
            with self.assertRaises(risk.GateError, msg=repr(value)):
                self.row(self.base, {"paper": value})
        left_out = {k: v for k, v in EXAMPLE.items() if k != "paper"}
        self.assertTrue(_check([self.base], left_out, at=self.at, offline=True)["paper"])

    def test_a_negative_limit_or_a_malformed_time_is_refused(self):
        for conf in ({"max_order_rupees": -1}, {"no_entries_before": "9:5"},
                     {"no_entries_after": "15:00:00"}, {"no_entries_before": 930}):
            with self.assertRaises(risk.GateError, msg=repr(conf)):
                self.row(self.base, conf)

    def test_max_positions_blocks_a_new_stock_but_not_adding_to_one(self):
        """config.example.json says the gate refuses a new position past max_positions."""
        held = {"cash": 10 ** 7, "positions": [{"symbol": s, "shares": 1, "cost": 100.0}
                                                for s in ("ITC", "INFY", "TCS", "RELIANCE", "HDFCBANK")]}
        conf = {"capital": 10 ** 7}
        new = check([self.base], conf, held, at=self.at, offline=True)["results"][0]
        self.assertTrue(any(r.startswith("positions: 5 already open") for r in new["reasons"]))
        more = dict(self.base, symbol="ITC")
        added = check([more], conf, held, at=self.at, offline=True)["results"][0]
        self.assertFalse(any(r.startswith("positions:") for r in added["reasons"]))
        off = check([self.base], dict(conf, max_positions=0), held, at=self.at,
                         offline=True)["results"][0]
        self.assertFalse(any(r.startswith("positions:") for r in off["reasons"]))

    def test_a_weekend_in_a_year_without_a_holiday_list_is_called_a_weekend(self):
        from nsetools import clock
        res = check([self.base], {}, at=clock.now("2027-01-09 11:00"), offline=True)
        self.assertTrue(any("a weekend" in r for r in res["results"][0]["reasons"]))

    # the command
    def test_a_config_that_cannot_be_read_is_exit_1_as_in_the_us_pack(self):
        code, _, err = self.cli({"nse/orders.json": {"orders": [self.base]},
                                 "config.json": "{ not json"}, "--now", "2026-10-22 11:00")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)

    def test_no_capital_and_no_portfolio_is_a_refusal_not_an_assumed_account(self):
        """As the US pack refuses with no budget and no portfolio: an assumed 1 lakh would pass or
        block on an account that is not the user's."""
        with self.assertRaises(risk.GateError):
            _check([self.base], {}, at=self.at, offline=True)
        code, out, err = self.cli({"nse/orders.json": {"orders": [self.base]}, "config.json": {}},
                                  "--now", "2026-10-22 11:00", "--no-write")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no capital in the config and no nse/portfolio.json", err)
        # The portfolio's own cash is the account then, and the figure is said.
        res = _check([self.base], {}, {"cash": 50000, "positions": []}, at=self.at, offline=True)
        self.assertTrue(any("Rs 50,000.00" in w for w in res["warnings"]))

    def test_a_now_it_cannot_read_is_exit_1_as_skill_md_says(self):
        code, _, err = self.cli({"nse/orders.json": {"orders": [self.base]}}, "--now", "garbage")
        self.assertEqual(code, 1)
        self.assertIn("--now wants a time", err)

    def test_gate_passes_without_fingerprints_each_count(self):
        """A recorded pass with no fingerprint cannot be told from another: each one counts, so the
        day's limits never undercount."""
        state = {"date": "2026-10-22", "passed": [
            {"symbol": s, "side": "buy", "kind": "entry", "max_loss": 10.0}
            for s in ("ITC", "INFY", "TCS")]}
        res = check([self.base], {}, at=self.at, offline=True, risk_state=state)
        self.assertEqual(res["today"]["entries"], 3)
        self.assertTrue(any(r.startswith("trades:") for r in res["results"][0]["reasons"]))

    def test_the_skill_md_example_is_what_the_command_prints(self):
        """The real SBIN order at 10:20 with the example config: every line SKILL.md shows."""
        code, out, _ = self.cli({"nse/orders.json": {"orders": [self.base]}},
                                "--now", "2026-10-22 10:20", "--no-write")
        self.assertEqual(code, 0)
        doc = (ROOT / "nse-risk" / "SKILL.md").read_text(encoding="utf-8")
        block = doc.split('$ python risk.py --now "2026-10-22 10:20"\n\n')[1].split("```")[0]
        self.assertEqual(block.strip(), out.strip())

    def test_the_skill_md_sizing_example_has_the_figures_it_prints(self):
        out = risk.size(687.40, 650.0, CONF, target=780.0)
        doc = (ROOT / "nse-risk" / "SKILL.md").read_text(encoding="utf-8")
        for label, value in (("costs, round trip", out["costs_round_trip"]),
                             ("loss if stopped", out["loss_if_stopped"]),
                             ("gain if target", out["gain_if_target"])):
            self.assertIn(f"{label:<22}{value:,.2f}", doc, label)

    def test_a_broker_log_that_is_not_an_object_is_refused(self):
        code, _, err = self.cli({"nse/orders.json": {"orders": [self.base]},
                                 "nse/state/broker.json": []}, "--now", "2026-10-22 11:00",
                                "--no-write")
        self.assertEqual(code, 1)
        self.assertIn("not a broker log", err)



if __name__ == "__main__":
    unittest.main(verbosity=2)
