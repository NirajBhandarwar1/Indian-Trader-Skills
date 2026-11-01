"""Position sizing. Pure arithmetic, so it is tested exactly and offline."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(folder: str, module: str):
    spec = importlib.util.spec_from_file_location(module, ROOT / folder / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


risk = _load("nse-risk", "risk")
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

    def test_gst_falls_on_brokerage_and_exchange_charges(self):
        c = risk.costs(100000.0, buying=True)
        expected = (c["brokerage"] + c["exchange"]) * 0.18
        self.assertAlmostEqual(c["gst"], expected, places=6)

    def test_every_component_is_present_and_adds_up(self):
        c = risk.costs(250000.0, buying=True)
        parts = ("brokerage", "stt", "exchange", "sebi", "stamp", "gst")
        for part in parts:
            self.assertIn(part, c)
        self.assertAlmostEqual(c["total"], sum(c[p] for p in parts), places=6)


class Defaults(unittest.TestCase):
    def test_it_runs_with_no_config_at_all(self):
        conf = risk.settings(Path("/definitely/not/here.json"))
        self.assertEqual(conf["capital"], 100000.0)
        self.assertEqual(conf["risk_per_trade_percent"], 1.0)

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

    def test_a_broken_config_does_not_stop_the_tool(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json at all")
            conf = risk.settings(path)
            self.assertEqual(conf["capital"], 100000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
