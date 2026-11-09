"""The order tickets, the yes, and the log, on the order a real run of the chain wrote.

The order is the real SBIN order of 2026-10-22 from nse-budget; its verdict comes from a real run of
nse-risk on it, made here, so the fingerprint the broker looks for is the one the gate recorded.
Nothing here talks to Zerodha: the skill never does.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import ROOT, fixture, has, skill                      # noqa: E402

broker = skill("nse-broker", "ticket")
risk = skill("nse-risk", "risk")

NOW = "2026-10-22 10:21"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = broker.main(argv)
    return code, out.getvalue() + err.getvalue()


@unittest.skipUnless(has("chain/orders.json"), "run capture.py")
class TheThreeGates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.orders = self.home / "orders.json"
        self.orders.write_text(json.dumps(fixture("chain/orders.json")), encoding="utf-8")
        self.risk = self.home / "risk.json"
        self.state = self.home / "broker.json"
        self.portfolio = self.home / "portfolio.json"
        # A real config, copied from the one users copy: a portfolio is only ever started from
        # the capital in a config, never from a default nobody gave.
        self.config = self.home / "config.json"
        self.config.write_text((ROOT / "config.example.json").read_text(encoding="utf-8"),
                               encoding="utf-8")
        self.check(NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, when):
        """The real gate on the real order, recorded where the broker reads it."""
        # A --portfolio named on the command line has to exist for the gate. With none yet, the
        # gate runs from the temp folder, so its default nse/portfolio.json is not there either
        # and it funds a fresh account from the config's capital.
        # The same for the broker log.
        named = [arg for flag, path in (("--portfolio", self.portfolio), ("--state", self.state))
                 if path.exists() for arg in (flag, str(path))]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.chdir(self.home):
            code = risk.main(["--orders", str(self.orders), "--now", when, "--offline",
                              "--out", str(self.risk), *named, "--config", str(self.config)])
        # A gate that refused to run must not pass silently as "not ready" in every test.
        self.assertTrue(self.risk.exists(), out.getvalue())
        return code

    def files(self, when=NOW):
        return ["--orders", str(self.orders), "--risk", str(self.risk), "--state",
                str(self.state), "--config", str(self.config), "--now", when]

    def test_the_real_order_is_ready_and_shows_both_calls(self):
        code, said = run(["show", *self.files()])
        self.assertEqual(code, 0)
        self.assertIn("READY: /nse-risk PASS", said)
        self.assertIn('"order_type": "MARKET"', said)
        self.assertIn('"order_type": "SL-M"', said)
        self.assertIn('"trigger_price": 950.05', said)
        self.assertIn("max loss about Rs 483.80", said)

    def test_the_entry_call_uses_zeradhas_own_parameter_names(self):
        """Checked against zerodha/kite-mcp-server mcp/post_tools.go."""
        call = broker.entry_call(fixture("chain/orders.json")["orders"][0])
        for key in ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity",
                    "product", "order_type"):
            self.assertIn(key, call)
        self.assertEqual((call["variety"], call["product"], call["quantity"]),
                         ("regular", "MIS", 41))

    def test_a_delivery_order_with_both_exits_gets_a_two_leg_gtt(self):
        order = dict(fixture("chain/orders.json")["orders"][0], product="CNC")
        tool, args = broker.protection_call(order, 41, 961.90)
        self.assertEqual((tool, args["trigger_type"]), ("place_gtt_order", "two-leg"))
        self.assertEqual((args["upper_trigger_value"], args["lower_trigger_value"]),
                         (985.45, 950.05))

    def test_without_a_risk_pass_it_is_not_ready(self):
        self.risk.unlink()
        code, said = run(["show", *self.files()])
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", said)

    def test_a_changed_order_is_not_the_one_that_passed(self):
        doc = json.loads(self.orders.read_text(encoding="utf-8"))
        doc["orders"][0]["quantity"] = 42
        self.orders.write_text(json.dumps(doc), encoding="utf-8")
        code, said = run(["show", *self.files()])
        self.assertIn("was not in the last risk check", said)

    def test_a_check_from_another_day_is_not_today(self):
        code, said = run(["show", *self.files("2026-10-23 10:21")])
        self.assertIn("not today", said)

    def test_only_an_explicit_yes_for_this_one_order_counts(self):
        for reply in ("sure", "ok", "yes to all", "yes ITC", "yes itc", "yes 50 shares",
                      "yes but wait", "yes?", ""):
            code, said = run(["approve", "--index", "1", "--reply", reply, *self.files()])
            self.assertNotIn("Recorded your yes", said, reply)
        self.assertFalse(self.state.exists())

    def test_a_plain_yes_is_recorded_word_for_word(self):
        code, said = run(["approve", "--index", "1", "--reply", "yes 41", *self.files()])
        self.assertEqual(code, 0)
        rec = json.loads(self.state.read_text(encoding="utf-8"))["orders"][-1]
        self.assertEqual((rec["status"], rec["reply"], rec["max_loss"]), ("approved", "yes 41",
                                                                          483.8))
        self.assertIn("PAPER: do NOT call place_order", said)

    def test_a_result_needs_a_yes_first(self):
        code, said = run(["result", "--index", "1", "--status", "placed", *self.files()])
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", said)

    def test_a_fill_lands_in_the_portfolio_and_the_gate_counts_it(self):
        run(["approve", "--index", "1", "--reply", "yes", *self.files()])
        code, said = run(["result", "--index", "1", "--status", "placed", "--filled-qty", "41",
                          "--filled-price", "961.90", "--portfolio", str(self.portfolio),
                          *self.files()])
        self.assertEqual(code, 0)
        pf = json.loads(self.portfolio.read_text(encoding="utf-8"))
        self.assertEqual(pf["cash"], 60562.1)                  # 1,00,000 - 41 x 961.90
        self.assertEqual(pf["positions"][0]["shares"], 41)
        self.assertIn("Now protect these 41 shares", said)
        # The same order again: the gate now knows it went out today.
        res = risk.check([fixture("chain/orders.json")["orders"][0]], {}, at=risk_clock(),
                         offline=True, broker_state=json.loads(self.state.read_text("utf-8")),
                         portfolio=pf)["results"][0]
        self.assertEqual(res["verdict"], "BLOCK")
        self.assertTrue(any(r.startswith("duplicate") for r in res["reasons"]))

    def test_a_result_that_cannot_record_its_fill_records_nothing(self):
        run(["approve", "--index", "1", "--reply", "yes", *self.files()])
        before = self.state.read_text(encoding="utf-8")
        code, said = run(["result", "--index", "1", "--status", "placed", "--filled-qty", "41.5",
                          "--filled-price", "961.90", "--portfolio", str(self.portfolio),
                          *self.files()])
        self.assertEqual(code, 1)
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)

    def test_flatten_sells_what_is_held_and_passes_the_gate_as_an_exit(self):
        run(["approve", "--index", "1", "--reply", "yes", *self.files()])
        run(["result", "--index", "1", "--status", "placed", "--filled-qty", "41",
             "--filled-price", "961.90", "--portfolio", str(self.portfolio), *self.files()])
        code, said = run(["flatten", "--symbol", "SBIN", "--orders", str(self.orders),
                          "--portfolio", str(self.portfolio), "--state", str(self.state),
                          "--config", str(self.config), "--now", "2026-10-22 15:15"])
        self.assertEqual(code, 0)
        self.assertIn("cancel_order", said)
        self.assertIn("Product MIS, from today's placed SBIN entry", said)
        doc = json.loads(self.orders.read_text(encoding="utf-8"))
        self.assertEqual((doc["orders"][0]["side"], doc["orders"][0]["quantity"]), ("sell", 41))
        self.check("2026-10-22 15:15")
        code, said = run(["show", *self.files("2026-10-22 15:15")])
        self.assertIn("READY", said)

    def test_nothing_to_flatten_is_said(self):
        code, said = run(["flatten", "--symbol", "SBIN", "--orders", str(self.orders),
                          "--portfolio", str(self.portfolio), "--state", str(self.state),
                          "--config", str(self.config)])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", said)

    # ---- the stop follows every fill, and only what was ordered can fill

    def approve_and_place(self, *extra):
        run(["approve", "--index", "1", "--reply", "yes", *self.files()])
        return run(["result", "--index", "1", "--status", "placed", "--portfolio",
                    str(self.portfolio), *extra, *self.files()])

    def fill(self, qty, side="buy", price="961.90"):
        return run(["fill", "--symbol", "SBIN", "--side", side, "--qty", str(qty), "--price", price,
                    "--portfolio", str(self.portfolio), "--state", str(self.state), "--config",
                    str(self.config), "--now", NOW])

    def test_placed_without_a_fill_says_no_stop_exists_and_each_fill_gets_its_own(self):
        code, said = self.approve_and_place("--broker-id", "250101000001")
        self.assertEqual(code, 0)
        self.assertIn("NO STOP EXISTS YET", said)
        code, said = self.fill(20)
        self.assertEqual(code, 0)
        self.assertIn('"order_type": "SL-M"', said)
        self.assertIn('"quantity": 20', said)
        code, said = self.fill(21)
        self.assertEqual(code, 0)
        self.assertIn('"quantity": 21', said)
        code, said = self.fill(1)
        self.assertEqual(code, 1)
        self.assertIn("at most 0 more can fill", said)
        pf = json.loads(self.portfolio.read_text(encoding="utf-8"))
        self.assertEqual(pf["positions"][0]["shares"], 41)

    def test_a_partial_fill_then_the_rest_each_get_a_stop(self):
        code, said = self.approve_and_place("--filled-qty", "20", "--filled-price", "961.90")
        self.assertIn('"quantity": 20', said)
        code, said = self.fill(22)
        self.assertEqual(code, 1)
        self.assertIn("at most 21 more", said)
        code, said = self.fill(21)
        self.assertEqual((code, '"quantity": 21' in said), (0, True))

    def test_a_fill_larger_than_the_order_is_refused(self):
        run(["approve", "--index", "1", "--reply", "yes", *self.files()])
        before = self.state.read_text(encoding="utf-8")
        code, said = run(["result", "--index", "1", "--status", "placed", "--filled-qty", "100",
                          "--filled-price", "961.90", "--portfolio", str(self.portfolio),
                          *self.files()])
        self.assertEqual(code, 1)
        self.assertIn("is for 41", said)
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)
        self.assertFalse(self.portfolio.exists())

    def test_a_stop_exit_fill_is_not_mistaken_for_more_of_the_entry(self):
        self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        code, said = self.fill(41, side="sell", price="950.05")
        self.assertEqual(code, 0)
        self.assertNotIn("Now protect", said)

    def test_the_gtt_stop_leg_limit_sits_one_percent_below_and_says_so(self):
        order = dict(fixture("chain/orders.json")["orders"][0], product="CNC")
        tool, args = broker.protection_call(order, 41, 961.90)
        self.assertEqual(args["lower_limit_price"], 940.50)       # 950.05 x 0.99, down to a tick
        self.assertEqual(args["upper_limit_price"], 985.45)
        words = " ".join(broker.protection_words(tool, args))
        self.assertIn("can stay unfilled", words)
        self.assertIn("change that limit", words)

    def test_a_short_intraday_entry_is_protected_by_a_buy_stop(self):
        order = dict(fixture("chain/orders.json")["orders"][0], side="sell")
        tool, args = broker.protection_call(order, 41, 961.90)
        self.assertEqual((tool, args["transaction_type"], args["order_type"], args["product"]),
                         ("place_order", "BUY", "SL-M", "MIS"))

    def test_a_short_entry_is_never_ready_but_an_exit_sell_is(self):
        for product in ("MIS", "CNC"):
            order = dict(fixture("chain/orders.json")["orders"][0], side="sell", product=product)
            fp = broker.fingerprint(order)
            for kind, ready in (("entry", False), ("exit", True)):
                gate = {"date": "2026-10-22", "results": [{"fingerprint": fp, "verdict": "PASS",
                                                           "kind": kind}]}
                ok, label, _ = broker.status_of(order, gate, {"orders": []}, "2026-10-22")
                self.assertEqual(ok, ready, (product, kind, label))
                if not ready:
                    self.assertIn("NOT READY: short entries are not supported: this pack's "
                                  "portfolio records only shares you hold, so the fill and its "
                                  "stop could not be tracked", label)
        cnc = dict(fixture("chain/orders.json")["orders"][0], side="sell", product="CNC")
        self.assertIsNone(broker.protection_call(cnc, 41, 961.90))

    def test_flatten_takes_the_product_of_todays_entry(self):
        self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        flatten = ["flatten", "--symbol", "SBIN", "--orders", str(self.orders), "--portfolio",
                   str(self.portfolio), "--state", str(self.state), "--config", str(self.config),
                   "--now", "2026-10-22 15:15"]
        code, said = run([*flatten, "--product", "CNC"])
        self.assertEqual(code, 1)
        self.assertIn("disagrees with today's placed SBIN entry, which was MIS", said)
        code, said = run(flatten)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.orders.read_text("utf-8"))["orders"][0]["product"], "MIS")

    def test_flatten_with_no_entry_recorded_says_it_fell_back_to_mis(self):
        code, said = run(["flatten", "--symbol", "SBIN", "--qty", "41", "--orders",
                          str(self.orders), "--state", str(self.state), "--config",
                          str(self.config), "--now", "2026-10-22 15:15"])
        self.assertEqual(code, 0)
        self.assertIn("the MIS default: no placed SBIN entry is recorded today", said)

    def test_the_skill_says_one_trade_per_yes_and_cash_only(self):
        text = (Path(__file__).resolve().parent.parent / "nse-broker" / "SKILL.md").read_text(
            encoding="utf-8")
        self.assertIn("A yes covers one trade: its entry and its own\n  protective stop, nothing "
                      "else.", text)
        self.assertNotIn("never two orders on one yes", text)
        self.assertIn("No options, margin, short selling or leverage", text)
        self.assertIn("sizes every order against cash only", text)
        self.assertIn("Each\n   partial fill gets its own stop", text)

    def test_a_decline_is_recorded_and_nothing_is_sent(self):
        code, said = run(["decline", "--index", "1", *self.files()])
        self.assertIn("Nothing was sent", said)
        self.assertEqual(json.loads(self.state.read_text("utf-8"))["orders"][-1]["status"],
                         "declined")

    def test_the_log_lists_the_day(self):
        run(["decline", "--index", "1", *self.files()])
        code, said = run(["log", "--state", str(self.state), "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("declined", said)


@unittest.skipUnless(has("chain/orders.json"), "run capture.py")
class HostileInputs(unittest.TestCase):
    """Each test takes the real chain order and changes one fact, or damages one file."""

    setUp, tearDown, check, files = (TheThreeGates.setUp, TheThreeGates.tearDown,
                                     TheThreeGates.check, TheThreeGates.files)
    approve_and_place, fill = TheThreeGates.approve_and_place, TheThreeGates.fill

    def order(self, **change):
        return dict(fixture("chain/orders.json")["orders"][0], **change)

    def flatten(self, *extra, when="2026-10-22 15:15"):
        return run(["flatten", "--symbol", "SBIN", "--orders", str(self.orders), "--portfolio",
                    str(self.portfolio), "--state", str(self.state), "--plan",
                    str(self.home / "plan.json"), "--config", str(self.config), "--now", when,
                    *extra])

    # ---- damaged files are refused by name, never a traceback, never written over

    def assert_refused(self, argv, words):
        code, said = run(argv)
        self.assertEqual(code, 1, said)
        self.assertNotIn("Traceback", said)
        self.assertIn(words, said)

    def test_a_damaged_broker_log_is_refused_and_left_as_it_is(self):
        for text in ("[]", '{"orders": "x"}', '{"orders": [1]}'):
            self.state.write_text(text, encoding="utf-8")
            self.assert_refused(["approve", "--index", "1", "--reply", "yes", *self.files()],
                                "is damaged")
            self.assert_refused(["log", "--state", str(self.state), "--now", NOW], "is damaged")
            self.assertEqual(self.state.read_text(encoding="utf-8"), text)

    def test_a_damaged_orders_risk_or_portfolio_file_is_refused(self):
        good = self.orders.read_text(encoding="utf-8")
        self.orders.write_text("[1, 2]", encoding="utf-8")
        self.assert_refused(["show", *self.files()], "order 1 is not an order object")
        self.orders.write_bytes(b"\xff\xfe\x00")
        self.assert_refused(["show", *self.files()], "is not valid JSON")
        self.orders.write_text(good, encoding="utf-8")
        self.risk.write_text('{"date": "2026-10-22", "results": [1]}', encoding="utf-8")
        self.assert_refused(["show", *self.files()], "is damaged")
        self.portfolio.write_text("[1]", encoding="utf-8")
        self.assert_refused(["fill", "--symbol", "SBIN", "--side", "buy", "--qty", "1", "--price",
                             "961.90", "--portfolio", str(self.portfolio), "--state",
                             str(self.state), "--config", str(self.config), "--now", NOW],
                            "is damaged")
        self.assertEqual(self.portfolio.read_text(encoding="utf-8"), "[1]")
        self.assert_refused(["flatten", "--symbol", "SBIN", "--orders", str(self.orders),
                             "--portfolio", str(self.portfolio), "--state", str(self.state),
                             "--config", str(self.config)], "is damaged")

    def test_an_order_with_a_bad_number_still_renders_as_not_ready(self):
        doc = json.loads(self.orders.read_text(encoding="utf-8"))
        doc["orders"][0]["quantity"] = "abc"
        self.orders.write_text(json.dumps(doc), encoding="utf-8")
        code, said = run(["show", *self.files()])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", said)
        self.assertIn("BUY 'abc' SBIN", said)
        self.assertIn("NOT READY", said)

    def test_impossible_dates_are_refused(self):
        self.assert_refused(["show", *self.files(), "--today", "2026-02-30"],
                            "--today wants a real date")
        self.assert_refused(["fill", "--symbol", "SBIN", "--side", "buy", "--qty", "1", "--price",
                             "961.90", "--date", "2026-13-45", "--portfolio",
                             str(self.portfolio), "--state", str(self.state), "--config",
                             str(self.config)], "--date wants a real date")
        self.assertFalse(self.portfolio.exists())

    def test_flatten_is_not_stopped_by_a_damaged_proposal_file(self):
        self.orders.write_text("garbage", encoding="utf-8")
        code, said = self.flatten("--qty", "41")
        self.assertEqual(code, 0, said)
        self.assertIn("it replaced a damaged file", said)
        self.assertEqual(json.loads(self.orders.read_text("utf-8"))["orders"][0]["quantity"], 41)

    def test_flatten_needs_a_symbol(self):
        code, said = run(["flatten", "--symbol", " ", "--qty", "41", "--orders", str(self.orders),
                          "--state", str(self.state), "--config", str(self.config)])
        self.assertEqual(code, 1)
        self.assertIn("--symbol wants the stock", said)

    # ---- the calls are spelled the way Zerodha spells them

    def test_a_lower_case_field_is_sent_upper_case(self):
        call = broker.entry_call(self.order(product="mis"))
        self.assertEqual(call["product"], "MIS")
        tool, args = broker.protection_call(self.order(product="mis"), 41, 961.90)
        self.assertEqual((tool, args["product"], args["order_type"]), ("place_order", "MIS",
                                                                       "SL-M"))
        self.assertEqual(broker.entry_call(self.order(order_type="market"))["order_type"],
                         "MARKET")

    def test_a_delivery_stop_alone_is_a_single_leg_gtt_not_a_day_order(self):
        tool, args = broker.protection_call(self.order(product="CNC", take_profit_price=None),
                                            41, 961.90)
        self.assertEqual((tool, args["trigger_type"], args["trigger_value"], args["limit_price"],
                          args["quantity"]), ("place_gtt_order", "single", 950.05, 940.50, 41))
        self.assertIn("no target leg", " ".join(broker.protection_words(tool, args)))

    def test_an_mtf_position_held_overnight_gets_a_gtt(self):
        tool, args = broker.protection_call(self.order(product="MTF"), 41, 961.90)
        self.assertEqual((tool, args["product"], args["trigger_type"]),
                         ("place_gtt_order", "MTF", "two-leg"))

    def test_a_gtt_with_no_price_known_leaves_last_price_out_and_says_so(self):
        order = self.order(product="CNC", plan=None)
        tool, args = broker.protection_call(order)
        self.assertNotIn("last_price", args)
        self.assertIn("last_price is left out", " ".join(broker.protection_words(tool, args)))

    # ---- the ticket's max loss is the gate's

    def test_the_max_loss_is_the_gates_figure_not_a_stale_plan_figure(self):
        doc = json.loads(self.orders.read_text(encoding="utf-8"))
        doc["orders"][0]["plan"]["max_loss"] = 400.0
        self.orders.write_text(json.dumps(doc), encoding="utf-8")
        self.check(NOW)
        code, said = run(["show", *self.files()])
        self.assertIn("max loss about Rs 483.80", said)
        self.assertNotIn("Rs 400", said)
        run(["approve", "--index", "1", "--reply", "yes", *self.files()])
        rec = json.loads(self.state.read_text(encoding="utf-8"))["orders"][-1]
        self.assertEqual(rec["max_loss"], 483.8)

    # ---- the yes

    def test_a_yes_naming_the_other_side_or_a_stray_number_is_not_a_yes(self):
        for reply in ("yes sell", "yes sell it", "yes 41 50"):
            code, said = run(["approve", "--index", "1", "--reply", reply, *self.files()])
            self.assertIn("Not recorded", said, reply)
        self.assertFalse(self.state.exists())
        code, said = run(["approve", "--index", "1", "--reply", "yes 41 shares at 950.05",
                          *self.files()])
        self.assertIn("Recorded your yes", said)

    def test_a_paper_ticket_does_not_say_place(self):
        code, said = run(["show", *self.files()])
        self.assertIn('Reply "yes" to record this one order on paper (nothing is sent)', said)
        self.assertIn("paper, so nothing is sent", said)

    def test_a_no_after_the_order_went_out_is_refused(self):
        self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        before = self.state.read_text(encoding="utf-8")
        code, said = run(["decline", "--index", "1", *self.files()])
        self.assertEqual(code, 1)
        self.assertIn("already placed", said)
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)

    # ---- fills and the portfolio

    def test_a_portfolio_started_from_nothing_says_where_its_cash_came_from(self):
        code, said = self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        self.assertIn(f"Started {self.portfolio} from your capital Rs 100,000 in "
                      f"{self.config}", said)

    def test_a_sale_that_was_not_the_stop_says_to_resize_the_stop(self):
        self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        code, said = self.fill(10, side="sell", price="970.00")
        self.assertEqual(code, 0)
        self.assertIn("31 SBIN still held", said)
        self.assertIn("change it to 31", said)

    def test_a_position_written_with_quantity_and_no_lots_keeps_its_shares_and_cost(self):
        self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        pf = json.loads(self.portfolio.read_text(encoding="utf-8"))
        row = pf["positions"][0]
        row["quantity"] = row.pop("shares")
        del row["lots"]
        self.portfolio.write_text(json.dumps(pf), encoding="utf-8")
        code, said = self.fill(10, side="sell", price="970.00")
        self.assertEqual(code, 0, said)
        row = json.loads(self.portfolio.read_text(encoding="utf-8"))["positions"][0]
        self.assertEqual(row["shares"], 31)
        self.assertNotIn("quantity", row)
        self.assertEqual(row["cost"], round(39437.9 - 39437.9 / 41 * 10, 2))

    def test_flatten_will_not_sell_more_as_mis_than_todays_mis_entry_bought(self):
        self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        pf = json.loads(self.portfolio.read_text(encoding="utf-8"))
        pf["positions"][0]["shares"] = 50                # 9 more held from before
        self.portfolio.write_text(json.dumps(pf), encoding="utf-8")
        code, said = self.flatten()
        self.assertEqual(code, 1)
        self.assertIn("more than today's MIS entry filled (41)", said)
        code, said = self.flatten("--qty", "41")
        self.assertEqual(code, 0, said)

    # ---- found in the side-by-side audit of 2026-10-25

    def test_no_capital_in_the_config_starts_no_portfolio(self):
        """As the US pack's ticket.py refuses: no default cash figure nobody gave."""
        self.config.write_text('{"paper": true}', encoding="utf-8")
        run(["approve", "--index", "1", "--reply", "yes", *self.files()])
        before = self.state.read_text(encoding="utf-8")
        code, said = run(["result", "--index", "1", "--status", "placed", "--filled-qty", "41",
                          "--filled-price", "961.90", "--portfolio", str(self.portfolio),
                          *self.files()])
        self.assertEqual(code, 1)
        self.assertIn("no capital (or budget) in the config to start it from", said)
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)
        self.assertFalse(self.portfolio.exists())
        code, said = self.fill(41)
        self.assertEqual(code, 1)
        self.assertFalse(self.portfolio.exists())
        self.config.write_text('{"budget": 50000}', encoding="utf-8")
        code, said = self.fill(41)
        self.assertEqual(code, 0, said)
        self.assertIn("from your budget Rs 50,000", said)

    def test_only_yes_or_y_opens_a_yes(self):
        for reply in ("haan", "ha", "ha ha", "haan 41"):
            code, said = run(["approve", "--index", "1", "--reply", reply, *self.files()])
            self.assertIn("Not recorded", said, reply)
        self.assertFalse(self.state.exists())

    def test_index_zero_is_refused_not_read_as_every_ticket(self):
        for argv in (["show", "--index", "0"], ["show", "--index", "0", "--json"]):
            self.assert_refused([*argv, *self.files()], "there is no order 0")

    def test_a_position_with_a_damaged_share_count_is_refused_not_overwritten(self):
        self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        pf = json.loads(self.portfolio.read_text(encoding="utf-8"))
        for shares, lots in (("abc", None), (10.5, None), (float("nan"), None),
                             (41, [{"id": "SBIN-1", "date": "2026-10-22", "shares": 50,
                                    "cost": 48095.0}])):
            row = dict(pf["positions"][0], shares=shares)
            if lots is not None:
                row["lots"] = lots
            text = json.dumps(dict(pf, positions=[row]))
            self.portfolio.write_text(text, encoding="utf-8")
            code, said = self.fill(10, side="sell", price="970.00")
            self.assertEqual(code, 1, (shares, said))
            self.assertIn("is damaged", said)
            self.assertEqual(self.portfolio.read_text(encoding="utf-8"), text)

    def test_flatten_of_shares_bought_before_today_is_never_an_mis_sell(self):
        self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        code, said = self.flatten(when="2026-10-23 10:30")         # the next trading day
        self.assertEqual(code, 1)
        self.assertIn("bought before today (2026-10-22)", said)
        self.assertIn("--product CNC", said)
        code, said = self.flatten("--product", "CNC", when="2026-10-23 10:30")
        self.assertEqual(code, 0, said)
        self.assertEqual(json.loads(self.orders.read_text("utf-8"))["orders"][0]["product"], "CNC")

    def test_flatten_takes_the_product_of_an_earlier_delivery_entry(self):
        doc = json.loads(self.orders.read_text(encoding="utf-8"))
        doc["orders"][0]["product"] = "CNC"
        self.orders.write_text(json.dumps(doc), encoding="utf-8")
        self.risk.unlink()               # setUp's pass of the MIS order would make this a duplicate
        self.check(NOW)
        self.approve_and_place("--filled-qty", "41", "--filled-price", "961.90")
        code, said = self.flatten(when="2026-10-23 10:30")
        self.assertEqual(code, 0, said)
        self.assertIn("Product CNC, from the placed SBIN delivery entry of 2026-10-22", said)

    def test_flatten_of_an_mis_entry_with_no_fill_recorded_does_not_sell_older_shares(self):
        self.approve_and_place("--broker-id", "250101000001")       # placed, no fill recorded
        self.portfolio.write_text(json.dumps({"cash": 50000, "positions": [
            {"symbol": "SBIN", "shares": 41, "lots": []}]}), encoding="utf-8")
        code, said = self.flatten()
        self.assertEqual(code, 1)
        self.assertIn("has no fill recorded", said)
        code, said = self.flatten("--qty", "41")
        self.assertEqual(code, 0, said)

    def test_a_damaged_plan_does_not_stop_a_flatten(self):
        (self.home / "plan.json").write_text("[1]", encoding="utf-8")
        code, said = self.flatten("--qty", "41")
        self.assertEqual(code, 0, said)
        self.assertIn("flat-by taken from the config instead", said)

    def test_an_exit_sell_carrying_a_stop_shows_no_buy_stop(self):
        order = self.order(side="sell")
        gate = {"date": "2026-10-22", "results": [{"fingerprint": broker.fingerprint(order),
                                                   "verdict": "PASS", "kind": "exit"}]}
        text, ready = broker.render(order, 1, 1, {"paper": True}, gate, {"orders": []},
                                    "2026-10-22")
        self.assertTrue(ready)
        self.assertNotIn("SL-M", text)
        self.assertNotIn("Once it fills", text)
        self.assertIsNone(broker.ticket_json(order, 1, {"paper": True}, gate, {"orders": []},
                                             "2026-10-22")["protection"])

    def test_a_bad_clock_or_config_is_one_line_and_exit_1(self):
        self.assert_refused(["show", *self.files(), "--now", "tomorrow"], "--now wants a time")
        self.assert_refused(["fill", "--symbol", "SBIN", "--side", "buy", "--qty", "1", "--price",
                             "961.90", "--now", "soon", "--portfolio", str(self.portfolio),
                             "--state", str(self.state), "--config", str(self.config)],
                            "--now wants a time")
        self.config.write_text("{not json", encoding="utf-8")
        self.assert_refused(["show", *self.files()], "is not valid JSON")

    def test_a_paper_switch_that_is_not_true_or_false_is_refused(self):
        conf = json.loads(self.config.read_text(encoding="utf-8"))
        for value in (None, "false", 0):
            self.config.write_text(json.dumps(dict(conf, paper=value)), encoding="utf-8")
            self.assert_refused(["show", *self.files()], "must be true or false")
            self.assert_refused(["approve", "--index", "1", "--reply", "yes", *self.files()],
                                "must be true or false")
        self.assertFalse(self.state.exists())

    def test_live_says_live_and_hands_over_the_exact_call(self):
        conf = json.loads(self.config.read_text(encoding="utf-8"))
        self.config.write_text(json.dumps(dict(conf, paper=False)), encoding="utf-8")
        code, said = run(["show", *self.files()])
        self.assertIn("LIVE ACCOUNT, REAL MONEY", said)
        self.assertIn("Allow once places it", said)
        code, said = run(["approve", "--index", "1", "--reply", "yes", *self.files()])
        self.assertEqual(code, 0)
        self.assertIn("Now call place_order with exactly:", said)
        self.assertNotIn("PAPER", said)

    def test_a_damaged_verdict_or_lot_is_refused_or_shown_never_a_traceback(self):
        gate = json.loads(self.risk.read_text(encoding="utf-8"))
        gate["results"][0].update(verdict="BLOCK", reasons=5, warnings="one note")
        self.risk.write_text(json.dumps(gate), encoding="utf-8")
        code, said = run(["show", *self.files()])
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED by /nse-risk: 5", said)
        self.assertIn("note: one note", said)
        self.portfolio.write_text(json.dumps({"cash": 1000, "positions": [
            {"symbol": "SBIN", "shares": 1, "lots": [1]}]}), encoding="utf-8")
        code, said = self.fill(1, side="sell", price="970.00")
        self.assertEqual(code, 1)
        self.assertIn("lots must be a list of objects", said)

    def test_the_skill_example_is_what_show_prints_and_the_read_only_list_is_whole(self):
        text = (ROOT / "nse-broker" / "SKILL.md").read_text(encoding="utf-8")
        example = text.split("## Example: the real SBIN trade")[1].split("```")[1]
        code, said = run(["show", *self.files()])
        for line in example.strip().splitlines():
            # The JSON is shown compacted; the note is /nse-risk's own words, which its owner
            # may reword, so only its presence is checked.
            if line.lstrip().startswith("note:"):
                self.assertIn("\n  note: ", said)
            elif not line.lstrip().startswith(("{", '"')):
                self.assertIn(line.rstrip(), said)
        self.assertIn("place_order,modify_order,cancel_order,place_gtt_order,modify_gtt_order,"
                      "delete_gtt_order", text)


def risk_clock():
    from nsetools import clock
    return clock.now("2026-10-22 10:30")


class FingerprintsAgree(unittest.TestCase):
    @unittest.skipUnless(has("chain/orders.json"), "run capture.py")
    def test_the_gate_and_the_broker_hash_an_order_the_same_way(self):
        order = fixture("chain/orders.json")["orders"][0]
        self.assertEqual(risk.fingerprint(order), broker.fingerprint(order))


if __name__ == "__main__":
    unittest.main(verbosity=2)
