"""Telling a desk turning its book from somebody taking a view.

Runs on real filings: 197 real PIT disclosures for INFY over a year, and 70 real bulk deals and
70 real block deals as the exchange served them.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load import FIXTURES, fixture, has, skill                  # noqa: E402

insider = skill("nse-insider", "insider")

PIT = "exchange/insider-INFY.json"
BULK = "exchange/bulk_deals.json"
BLOCK = "exchange/block_deals.json"
FLOWS = "exchange/fii-dii.json"


@unittest.skipUnless(has(PIT), "run capture.py")
class RealPitFilings(unittest.TestCase):
    """The Indian answer to watching congressional disclosures, and a faster one."""

    def setUp(self):
        self.rows = fixture(PIT)

    def test_a_year_of_real_filings_came_back(self):
        self.assertEqual(len(self.rows), 197)

    def test_a_symbol_is_required_in_practice(self):
        """Asked market-wide the endpoint answers 200 with an empty list, which reads as
        'no insider activity anywhere'. Asked for INFY it returns 197 filings."""
        self.assertIn("A SYMBOL IS REQUIRED", insider.insiders.__doc__
                      or __import__("nsetools.nse", fromlist=["nse"]).insider_trades.__doc__)

    def test_every_real_filing_carries_the_fields_the_skill_reads(self):
        for row in self.rows:
            for key in ("acqName", "acqMode", "befAcqSharesNo", "afterAcqSharesNo"):
                self.assertIn(key, row)

    def test_the_real_dates_parse(self):
        for row in self.rows:
            self.assertTrue(insider.day_of(row.get("acqtoDt") or row.get("acqfromDt") or ""))

    def test_esop_allotments_are_the_commonest_real_mode(self):
        """Most insider filings are pay, not conviction. That is why mode matters."""
        modes = [(r.get("acqMode") or "").lower() for r in self.rows]
        self.assertTrue(any("esop" in m for m in modes))

    def test_the_telling_modes_are_the_open_market_ones(self):
        """A market purchase is somebody choosing to buy. An ESOP allotment is a salary."""
        for mode in insider.TELLING:
            self.assertTrue(any(word in mode for word in ("market", "off market")))

    def test_nil_is_read_as_nothing_not_as_a_number(self):
        """'Nil' appears in the real before-and-after share columns."""
        nils = [r for r in self.rows if str(r.get("befAcqSharesNo")).strip().lower() == "nil"]
        self.assertTrue(nils, "no Nil in this capture")
        self.assertIsNone(insider.rupees("Nil"))


@unittest.skipUnless(has(BULK) and has(BLOCK), "run capture.py")
class RealBulkAndBlockDeals(unittest.TestCase):
    """These name the buyer, which is the thing no US equivalent gives you."""

    def setUp(self):
        self.bulk = fixture(BULK)
        self.block = fixture(BLOCK)

    def test_both_kinds_came_back(self):
        self.assertGreater(len(self.bulk), 20)
        self.assertGreater(len(self.block), 20)

    def test_the_date_field_is_the_prefixed_one(self):
        """Every column is prefixed BD_, including the date. Guessing 'date' returns nothing."""
        for row in self.bulk:
            self.assertIn("BD_DT_DATE", row)
            self.assertNotIn("date", row)

    def test_every_real_deal_names_a_client(self):
        for row in self.bulk + self.block:
            self.assertTrue((row.get("BD_CLIENT_NAME") or "").strip())

    def test_every_real_deal_says_which_side_it_was(self):
        for row in self.bulk + self.block:
            self.assertIn((row.get("BD_BUY_SELL") or "").upper(), ("BUY", "SELL"))

    def test_the_real_dates_parse(self):
        for row in self.bulk:
            self.assertTrue(insider.day_of(row["BD_DT_DATE"]))


@unittest.skipUnless(has(BULK), "run capture.py")
class RoundTripsInRealDeals(unittest.TestCase):
    """A desk that bought and sold the same stock the same day took no view at all."""

    def real_deal(self, **over) -> dict:
        """One real deal from the exchange, in the shape deals() builds."""
        row = fixture(BULK)[0]
        shaped = {"date": insider.day_of(row["BD_DT_DATE"]),
                  "symbol": row["BD_SYMBOL"], "client": row["BD_CLIENT_NAME"],
                  "side": (row["BD_BUY_SELL"] or "").upper(),
                  "shares": insider.rupees(row["BD_QTY_TRD"]),
                  "price": insider.rupees(row["BD_TP_WATP"]), "value_crore": 1.0}
        shaped.update(over)
        return shaped

    def test_a_client_on_both_sides_of_one_symbol_is_flagged(self):
        """The test is being on both sides at all, not the legs matching in size.

        A 10% band would let LAXMI TRADE SOLUTIONS through on the real 24 September HEROMOTORS
        deals: 2,226,406 bought against 2,552,391 sold, 13% apart.
        """
        rows = [self.real_deal(side="BUY", shares=2226406),
                self.real_deal(side="SELL", shares=2552391)]
        insider.mark_round_trips(rows)
        self.assertTrue(all(r["round_trip"] for r in rows))
        self.assertIn("both sides", rows[0]["note"])

    def test_legs_that_are_flat_by_the_close_say_so(self):
        """MICROCURVES really bought and sold 313,241 GNA on 24 September 2026."""
        rows = [self.real_deal(side="BUY", shares=313241),
                self.real_deal(side="SELL", shares=313241)]
        insider.mark_round_trips(rows)
        self.assertEqual(rows[0]["note"], "both sides, flat by the close")
        self.assertEqual(rows[0]["net_shares"], 0)

    def test_a_client_on_one_side_only_is_not_flagged(self):
        rows = [self.real_deal(side="BUY")]
        insider.mark_round_trips(rows)
        self.assertFalse(rows[0]["round_trip"])
        self.assertEqual(rows[0]["note"], "")

    def test_different_clients_on_opposite_sides_are_not_a_round_trip(self):
        rows = [self.real_deal(side="BUY"),
                self.real_deal(side="SELL", client="SOMEBODY ELSE")]
        insider.mark_round_trips(rows)
        self.assertFalse(any(r["round_trip"] for r in rows))

    def test_the_same_client_in_two_different_stocks_is_not_a_round_trip(self):
        rows = [self.real_deal(side="BUY"),
                self.real_deal(side="SELL", symbol="SOMETHINGELSE")]
        insider.mark_round_trips(rows)
        self.assertFalse(any(r["round_trip"] for r in rows))

    def test_nothing_is_dropped_only_labelled(self):
        """The deal did happen and the price is real, so it stays on the list."""
        rows = [self.real_deal(side="BUY", shares=1000),
                self.real_deal(side="SELL", shares=1000)]
        insider.mark_round_trips(rows)
        self.assertEqual(len(rows), 2)


@unittest.skipUnless(has(FLOWS), "run capture.py")
class RealInstitutionalFlow(unittest.TestCase):
    def test_the_real_rows_name_both_sides(self):
        rows = fixture(FLOWS)
        self.assertTrue(rows)
        text = " ".join(str(r) for r in rows).upper()
        self.assertIn("FII", text)
        self.assertIn("DII", text)


class Rupees(unittest.TestCase):
    """Amounts arrive as strings with commas, and sometimes as words."""

    def test_a_plain_number(self):
        self.assertEqual(insider.rupees("1234"), 1234.0)

    def test_commas_are_indian_grouping(self):
        self.assertEqual(insider.rupees("2,06,100"), 206100.0)

    def test_nil_and_blank_are_nothing_not_zero(self):
        for raw in ("Nil", "", "-", None):
            self.assertIsNone(insider.rupees(raw))


EXCHANGE = str(FIXTURES / "exchange")
TODAY = date(2026, 10, 24)


@unittest.skipUnless(has(PIT), "run capture.py")
class EveryTradeIsItsOwnRow(unittest.TestCase):
    def test_one_filing_can_carry_several_trades_and_none_is_lost(self):
        """INFY's 197 rows are 27 filings, one of them 102 trades. Keying on the filing would have
        shown one trade and silently marked the rest as already seen."""
        raw = fixture(PIT)
        self.assertEqual(len({r["pid"] for r in raw}), 27)
        rows = insider.insiders("INFY", 365, 0, TODAY, EXCHANGE)
        self.assertEqual(len(rows), 197)
        self.assertEqual(len({r["id"] for r in rows}), 197)


@unittest.skipUnless(has(PIT), "run capture.py")
class HowLongItTookToDisclose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = insider.insiders("INFY", 365, 0, TODAY, EXCHANGE)

    def by(self, who, traded):
        return next(r for r in self.rows if r["who"].startswith(who) and r["traded"] == traded)

    def test_trading_days_skip_weekends_and_exchange_holidays(self):
        """Traded Wednesday 18 March 2026, the company told Monday 23 March: the 19th, the 20th
        and the 23rd. The 21st was a Saturday, and Id-Ul-Fitr besides."""
        row = self.by("Venkateshwaran", "2026-03-18")
        self.assertEqual(row["to_company_trading_days"], 3)
        self.assertEqual(row["to_exchange_trading_days"], 0)
        self.assertEqual(row["days_to_publish"], 5)

    def test_no_row_is_called_late(self):
        """The two-day clock starts when a quarter's trades reach Rs 10 lakh, which a row cannot
        show: this January trade was published with one of 2 February, a month after it."""
        row = self.by("Mitrankur", "2026-01-05")
        self.assertEqual(row["days_to_publish"], 31)
        for r in self.rows:
            self.assertNotIn("late", r)

    def test_a_year_whose_holidays_are_not_known_is_not_counted(self):
        """The exchange publishes only this year's list. Counting 2025 as if it had no holidays
        would be wrong sixteen times a year."""
        old = [r for r in self.rows if r["filed"] < "2026-01-01"
               and r["told_company"] and r["told_company"] < r["filed"]]
        self.assertTrue(old)
        self.assertTrue(all(r["to_exchange_trading_days"] is None for r in old))
        self.assertTrue(all(r["days_to_publish"] is not None for r in old))
        # Told and published the same day is no time at all, whatever the calendar says.
        same = [r for r in self.rows if r["filed"] < "2026-01-01" and r["told_company"] == r["filed"]]
        self.assertTrue(all(r["to_exchange_trading_days"] == 0 for r in same))

    def test_the_window_is_counted_back_from_today(self):
        rows = insider.insiders("INFY", 30, 0, date(2026, 4, 30), EXCHANGE)
        self.assertTrue(rows)
        self.assertTrue(all("2026-03-31" <= r["filed"] <= "2026-04-30" for r in rows))


@unittest.skipUnless(has(BULK), "run capture.py")
class OnlyWhatIsNewAndWhatIsYours(unittest.TestCase):
    def run_it(self, *args, state):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = insider.main(["--offline", EXCHANGE, "--today", "2026-10-24", "--state",
                                 str(state), *args])
        return code, out.getvalue()

    def test_the_second_check_has_nothing_new_and_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            first, _ = self.run_it("--bulk", "--new", "--top", "100", state=state)
            second, said = self.run_it("--bulk", "--new", "--top", "100", state=state)
            self.assertEqual((first, second), (0, 1))
            self.assertIn("Nothing new", said)

    def test_a_dry_run_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            self.run_it("--bulk", "--new", "--dry-run", state=state)
            self.assertFalse(state.exists())

    def test_insider_and_deal_feeds_are_remembered_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            self.run_it("--bulk", "--new", state=state)
            code, _ = self.run_it("--insiders", "--symbol", "INFY", "--days", "365", "--new",
                                  state=state)
            self.assertEqual(code, 0)
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(set(saved["seen"]), {"bulk_deals", "insider"})

    def test_deals_in_the_named_stocks_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, said = self.run_it("--bulk", "--tickers", "AJAXENGG", "--json",
                                     state=Path(tmp) / "s.json")
            rows = json.loads(said)
            flagged = {r["symbol"] for r in rows if r["yours"]}
            self.assertEqual(flagged, {"AJAXENGG"})

    def test_asking_about_a_company_does_not_make_it_yours(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said = self.run_it("--insiders", "--symbol", "INFY", "--days", "365", "--json",
                                  "--portfolio", str(Path(tmp) / "none.json"),
                                  "--watchlist", str(Path(tmp) / "none.json"),
                                  "--plan", str(Path(tmp) / "none.json"),
                                  state=Path(tmp) / "s.json")
            self.assertFalse(any(r["yours"] for r in json.loads(said)))

    def test_since_keeps_only_the_later_filings(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said = self.run_it("--bulk", "--since", "2026-09-24", "--json",
                                  state=Path(tmp) / "s.json")
            rows = json.loads(said)
            self.assertTrue(rows)
            self.assertTrue(all(r["date"] >= "2026-09-24" for r in rows))

    def test_one_person_can_be_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said = self.run_it("--insiders", "--symbol", "INFY", "--days", "365", "--json",
                                  "--member", "mitrankur", state=Path(tmp) / "s.json")
            rows = json.loads(said)
            self.assertEqual(len(rows), 3)          # 20 Nov 2025, 5 Jan and 2 Feb 2026
            self.assertTrue(all(r["who"].startswith("Mitrankur") for r in rows))

    def test_a_mode_can_be_picked(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said = self.run_it("--insiders", "--symbol", "INFY", "--days", "365", "--json",
                                  "--type", "Market Sale", state=Path(tmp) / "s.json")
            rows = json.loads(said)
            self.assertEqual(len(rows), 26)
            self.assertEqual({r["mode"] for r in rows}, {"Market Sale"})

    def test_a_calendar_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said = self.run_it("--insiders", "--symbol", "INFY", "--json", "--year", "2025",
                                  state=Path(tmp) / "s.json")
            rows = json.loads(said)
            self.assertTrue(rows)
            self.assertTrue(all(r["filed"].startswith("2025") for r in rows))

    def test_the_portfolio_is_read_for_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "f.json").write_text(json.dumps({"positions": [{"symbol": "INFY"}]}),
                                         encoding="utf-8")
            self.assertEqual(insider.watch_from_files(home / "f.json", home / "w.json",
                                                      home / "p.json"), ["INFY"])


def _run(*args, home: Path, offline: str = EXCHANGE):
    """main() on saved answers, with a portfolio, plan and watchlist that do not exist."""
    out, err = io.StringIO(), io.StringIO()
    none = str(home / "none.json")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = insider.main(["--offline", offline, "--today", "2026-10-24",
                             "--state", str(home / "state.json"), "--portfolio", none,
                             "--watchlist", none, "--plan", none, *args])
    return code, out.getvalue(), err.getvalue()


def _copy_of_infy(home: Path, change=None) -> str:
    """A folder holding the real INFY answer, with one fact changed if asked."""
    body = json.loads((FIXTURES / PIT).read_text(encoding="utf-8"))
    if change:
        change(body["data"])
    folder = home / "saved"
    folder.mkdir()
    (folder / "insider-INFY.json").write_text(json.dumps(body), encoding="utf-8")
    return str(folder)


@unittest.skipUnless(has(PIT), "run capture.py")
class WhichWayEachInsiderTradeWent(unittest.TestCase):
    """Fix 1. The mode does not say buy or sell: 72 of INFY's ESOP rows are sales."""

    @classmethod
    def setUpClass(cls):
        cls.rows = {r["id"]: r for r in insider.insiders("INFY", 365, 0, TODAY, EXCHANGE)}

    def test_the_filings_own_transaction_type_wins(self):
        sells = [r for r in self.rows.values() if r["mode"] == "ESOP" and r["side"] == "SELL"]
        self.assertEqual(len(sells), 72)
        self.assertTrue(all(r["side_from"] == "filing" for r in sells))

    def test_a_blank_type_is_read_from_the_holding_before_and_after(self):
        """570209: 8,668,508 -> 8,650,911 shares. 570210: Nil -> 2,061, Nil being none."""
        self.assertEqual(self.rows["pit:570209"]["side"], "SELL")
        self.assertEqual(self.rows["pit:570210"]["side"], "BUY")
        self.assertEqual(self.rows["pit:570208"]["side"], "BUY")
        self.assertEqual(self.rows["pit:570209"]["side_from"], "holding before and after")

    def test_neither_saying_is_unknown_not_a_guess(self):
        row = dict(fixture(PIT)[0], tdpTransactionType="-", afterAcqSharesNo="Nil")
        self.assertEqual(insider.side_of(row), ("unknown", ""))

    def test_the_side_is_a_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "3",
                              home=Path(tmp))
            self.assertIn(" side ", said)
            self.assertIn("SELL", said)


@unittest.skipUnless(has(PIT), "run capture.py")
class TheTradeDateTheSecurityAndTheLink(unittest.TestCase):
    """Fixes 2 and 8."""

    def test_trade_date_and_security_are_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "2",
                              home=Path(tmp))
            self.assertIn("traded", said)
            self.assertIn("2026-03-30", said)        # 570209 traded 30 March, filed 6 April
            self.assertIn("ADR/GDR/FCCB", said)

    def test_each_new_alert_carries_its_filing_and_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "1",
                              "--new", home=Path(tmp))
            self.assertIn("transaction 570209, filing 1197666, https://nsearchives", said)

    def test_a_trade_over_several_days_shows_both_and_is_measured_from_the_last(self):
        """The real 570209 traded 30 March; moved to start on the 25th it covers six days."""
        def spread(rows):
            next(r for r in rows if r["did"] == "570209")["acqfromDt"] = "25-Mar-2026"
        with tempfile.TemporaryDirectory() as tmp:
            saved = _copy_of_infy(Path(tmp), spread)
            row = next(r for r in insider.insiders("INFY", 365, 0, TODAY, saved)
                       if r["id"] == "pit:570209")
            self.assertEqual(row["traded"], "2026-03-25 to 2026-03-30")
            self.assertEqual((row["traded_from"], row["traded_to"]), ("2026-03-25", "2026-03-30"))
            self.assertEqual(row["days_to_publish"], 7)            # 30 March to 6 April
            self.assertIn("LAST day of the trade", insider.INSIDER_NOTE)


@unittest.skipUnless(has(PIT), "run capture.py")
class AFailedReadIsNotNothingNew(unittest.TestCase):
    """Fix 3. A block or a missing answer must never read as 'nothing new'."""

    def test_every_symbol_failing_exits_two_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, said, _ = _run("--insiders", "--symbol", "TCS", "--new", home=Path(tmp))
            self.assertEqual(code, 2)
            self.assertIn("insider-TCS.json", said)
            self.assertNotIn("Nothing new", said)
            self.assertNotIn("No filings", said)

    def test_one_failing_is_named_and_the_rest_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, said, _ = _run("--insiders", "--tickers", "INFY", "TCS", "--days", "365",
                                 home=Path(tmp))
            self.assertEqual(code, 0)
            self.assertTrue(said.startswith("INFY insider"))
            self.assertIn("not read, so nothing here is about it: TCS", said)


@unittest.skipUnless(has(BLOCK), "run capture.py")
class AskingAboutADealIsNotOwningIt(unittest.TestCase):
    """Fix 4. --symbol SANOFI flagged both real SANOFI block deals as yours."""

    def test_symbol_does_not_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--block", "--symbol", "SANOFI", "--today", "2026-10-02", "--json",
                              home=Path(tmp))
            rows = json.loads(said)
            self.assertEqual(len(rows), 2)
            self.assertFalse(any(r["yours"] for r in rows))

    def test_tickers_and_the_portfolio_still_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "pf.json").write_text(json.dumps({"positions": [{"symbol": "SANOFI"}]}),
                                          encoding="utf-8")
            _, said, _ = _run("--block", "--symbol", "SANOFI", "--today", "2026-10-02", "--json",
                              "--portfolio", str(home / "pf.json"), home=home)
            self.assertTrue(all(r["yours"] for r in json.loads(said)))


@unittest.skipUnless(has(PIT) and has(BULK) and has(BLOCK), "run capture.py")
class MachineReadableAndEveryFeed(unittest.TestCase):
    """Fixes 5 and 6."""

    def test_json_with_save_is_still_json(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            _, said, err = _run("--insiders", "--symbol", "INFY", "--days", "365", "--json",
                                "--save", "--top", "2", home=Path(tmp))
            self.assertEqual(len(json.loads(said)), 2)
            self.assertIn("wrote", err)
            self.assertEqual(len(json.loads((Path(tmp) / "nse" / "insider.json")
                                            .read_text(encoding="utf-8"))), 197)

    def test_every_feed_asked_for_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--bulk", "--block", "--flows", "--today", "2026-10-02",
                              "--top", "1", home=Path(tmp))
            for title in ("Bulk deals,", "Block deals,", "Institutional flow"):
                self.assertIn(title, said)

    def test_several_feeds_in_json_are_one_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--bulk",
                              "--json", home=Path(tmp))
            self.assertEqual(set(json.loads(said)), {"insider", "bulk_deals"})


@unittest.skipUnless(has(PIT), "run capture.py")
class CountsAndWindowsSaidPlainly(unittest.TestCase):
    """Fix 7."""

    def test_found_and_matched_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365",
                              "--member", "atul", "--type", "esop", home=Path(tmp))
            self.assertIn("197 found, 1 match your filters", said)

    def test_a_filter_that_empties_the_list_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365",
                              "--type", "Market Purchase", home=Path(tmp))
            self.assertIn("Nothing matched your filters", said)
            self.assertNotIn("No filings", said)

    def test_a_year_is_labelled_by_its_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--year", "2025", home=Path(tmp))
            self.assertIn("2025-01-01 to 2025-12-31. 68 found", said)
            self.assertNotIn("days", said.splitlines()[0])

    def test_since_is_labelled_by_its_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--since", "2026-03-20",
                              home=Path(tmp))
            self.assertIn("2026-03-20 to 2026-10-24. 7 found", said)

    def test_days_and_top_below_one_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for flag in ("--days", "--top"):
                for bad in ("0", "-5"):
                    code, said, _ = _run("--insiders", "--symbol", "INFY", flag, bad,
                                         home=Path(tmp))
                    self.assertEqual(code, 2, (flag, bad))
                    self.assertIn("wants 1 or more", said)


@unittest.skipUnless(has(PIT), "run capture.py")
class DerivativesAndModes(unittest.TestCase):
    """Fixes 9 and 10."""

    def test_a_derivative_trade_is_surfaced(self):
        """Every real INFY row says '-'. One changed to an options trade must show."""
        def options(rows):
            row = next(r for r in rows if r["did"] == "570209")
            row["derivativeType"], row["tdpDerivativeContractType"] = "Options", "Call"
        with tempfile.TemporaryDirectory() as tmp:
            saved = _copy_of_infy(Path(tmp), options)
            rows = {r["id"]: r for r in insider.insiders("INFY", 365, 0, TODAY, saved)}
            self.assertEqual(rows["pit:570209"]["derivative"], "Options Call")
            self.assertIsNone(rows["pit:570210"]["derivative"])
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "1",
                              home=Path(tmp), offline=saved)
            self.assertIn("derivative", said)
            self.assertIn("Options Call", said)

    def test_no_derivative_column_when_none_is_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "5",
                              home=Path(tmp))
            self.assertNotIn("derivative ", said)

    def test_off_market_is_not_counted_as_telling(self):
        """15 real INFY Off Market rows; most are the employee trust passing shares on."""
        rows = insider.insiders("INFY", 365, 0, TODAY, EXCHANGE)
        off = [r for r in rows if r["mode"] == "Off Market"]
        self.assertEqual(len(off), 15)
        self.assertFalse(any(r["telling"] for r in off))
        self.assertTrue(all(r["telling"] for r in rows if r["mode"] == "Market Sale"))


# ---------------------------------------------------------------- the second side-by-side pass

# The header of the exchange's deals CSV export, as the insider-activity agent copied it from a live
# answer: a byte order mark, trailing spaces and slashes in the names.
CSV_HEADER = ('﻿"Date ","Symbol ","Security Name ","Client Name ","Buy / Sell ",'
              '"Quantity Traded ","Trade Price / Wght. Avg. Price ","Remarks "')
# One real short-selling row, as the insider-activity agent copied it from a live answer.
SHORT_ROW = {"SS_DATE": "09-SEP-2026", "SS_DATE_ORDER": "2026-09-08T18:30:00.000Z",
             "SS_SYMBOL": "ADANIENSOL", "SS_NAME": "ADANI ENERGY SOLUTION LTD", "SS_QTY": 1500}


def _as_csv(rows) -> str:
    """The real JSON deals written out in the CSV export's layout, fact for fact."""
    lines = [CSV_HEADER]
    for r in rows:
        cells = [r["BD_DT_DATE"], r["BD_SYMBOL"], r["BD_SCRIP_NAME"], r["BD_CLIENT_NAME"],
                 r["BD_BUY_SELL"], f"{r['BD_QTY_TRD']:,}", str(r["BD_TP_WATP"]),
                 r["BD_REMARKS"] or "-"]
        lines.append(",".join('"' + c.replace('"', '""') + '"' for c in cells))
    return "\n".join(lines) + "\n"


def _folder(home: Path, files: dict) -> str:
    folder = home / "saved"
    folder.mkdir(exist_ok=True)
    for name, body in files.items():
        (folder / name).write_text(body if isinstance(body, str) else json.dumps(body),
                                   encoding="utf-8")
    return str(folder)


@unittest.skipUnless(has(BULK), "run capture.py")
class TheSeventyRowCap(unittest.TestCase):
    """The deals endpoint's JSON stops at 70 rows; an average day has about 110 bulk deals."""

    def test_a_json_answer_of_seventy_is_not_called_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--bulk", "--today", "2026-10-02", "--top", "1", home=Path(tmp))
            self.assertIn("At least 70 disclosed", said)
            self.assertIn("stops at 70 rows", said)

    def test_the_csv_export_is_read_whole_and_says_no_cap(self):
        raw = fixture(BULK)
        with tempfile.TemporaryDirectory() as tmp:
            saved = _folder(Path(tmp), {"bulk_deals.csv": _as_csv(raw)})
            _, said, _ = _run("--bulk", "--today", "2026-10-02", "--top", "1", home=Path(tmp),
                              offline=saved)
            self.assertIn("70 disclosed", said)
            self.assertNotIn("At least", said)
            from_csv = insider.deals("bulk_deals", 30, None, 0, date(2026, 10, 2), saved)
            from_json = insider.deals("bulk_deals", 30, None, 0, date(2026, 10, 2), EXCHANGE)
            self.assertEqual([r["id"] for r in from_csv], [r["id"] for r in from_json])
            self.assertEqual([r["value_crore"] for r in from_csv],
                             [r["value_crore"] for r in from_json])

    def test_an_export_missing_a_column_is_refused_not_read_blank(self):
        with self.assertRaises(insider.NotPublished):
            insider.nse.deals_from_csv(b"<html>holding page</html>\n")
        text = _as_csv(fixture(BULK)[:1]).replace("Client Name", "Someone")
        with self.assertRaises(insider.NotPublished):
            insider.nse.deals_from_csv(text)

    def test_live_bulk_deals_ask_for_the_csv(self):
        asked = []
        real = insider.nse.fetch

        def fake(url, referer=None, timeout=30):
            asked.append(url)
            return _as_csv(fixture(BULK)).encode("utf-8")
        insider.nse.fetch = fake
        try:
            rows = insider.nse.bulk_block_deals("bulk_deals", 30, date(2026, 10, 2))
        finally:
            insider.nse.fetch = real
        self.assertTrue(asked[0].endswith("&csv=true"))
        self.assertEqual(len(rows), 70)
        self.assertEqual(rows[0]["BD_SYMBOL"], "AASTHA")

    def test_a_missing_price_is_unknown_not_zero(self):
        raw = fixture(BULK)
        raw[0] = dict(raw[0], BD_TP_WATP=None)
        with tempfile.TemporaryDirectory() as tmp:
            saved = _folder(Path(tmp), {"bulk_deals.json": raw})
            rows = insider.deals("bulk_deals", 30, "AASTHA", 0, date(2026, 10, 2), saved)
            row = next(r for r in rows if r["client"] == "JAGID VANITABEN RAJENDRAPRASAD"
                       and r["side"] == "BUY")
            self.assertIsNone(row["value_crore"])
            self.assertIsNone(row["price"])
            self.assertEqual(row["shares"], 627427)


class ShortSelling(unittest.TestCase):
    """Short-selling rows are SS_ fields with no client, side or price. Read as BD_ rows, every
    one came out blank and was silently outside the window: "nothing disclosed"."""

    def test_the_real_row_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = _folder(Path(tmp), {"short_selling.json": [SHORT_ROW]})
            rows = insider.deals("short_selling", 30, None, 0, date(2026, 9, 10), saved)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row["symbol"], row["date"], row["shares"]),
                             ("ADANIENSOL", "2026-09-09", 1500))
            self.assertIsNone(row["value_crore"])
            self.assertEqual((row["client"], row["side"], row["round_trip"]), ("", "", False))
            code, said, _ = _run("--short", "--today", "2026-09-10", "--member", "x",
                                 home=Path(tmp), offline=saved)
            self.assertEqual(code, 0)
            self.assertIn("ADANIENSOL", said)
            self.assertIn("shares sold short", said)
            self.assertIn("--member does not apply", said)

    def test_a_row_without_a_symbol_is_counted_not_dropped_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = _folder(Path(tmp), {"short_selling.json": [dict(SHORT_ROW, SS_SYMBOL="")]})
            code, said, _ = _run("--short", "--today", "2026-09-10", home=Path(tmp),
                                 offline=saved)
            self.assertEqual(code, 1)
            self.assertIn("1 rows had no readable symbol, date or quantity", said)


@unittest.skipUnless(has(PIT), "run capture.py")
class NothingSetAsideSilently(unittest.TestCase):
    def test_an_undated_filing_is_counted(self):
        def blank(rows):
            next(r for r in rows if r["did"] == "570209")["date"] = "-"
        with tempfile.TemporaryDirectory() as tmp:
            saved = _copy_of_infy(Path(tmp), blank)
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "1",
                              home=Path(tmp), offline=saved)
            self.assertIn("196 found", said)
            self.assertIn("1 filing rows carried no readable date", said)

    def test_a_row_for_another_company_is_not_reported_as_this_one(self):
        def other(rows):
            next(r for r in rows if r["did"] == "570209")["symbol"] = "TCS"
        with tempfile.TemporaryDirectory() as tmp:
            saved = _copy_of_infy(Path(tmp), other)
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "1",
                              home=Path(tmp), offline=saved)
            self.assertIn("196 found", said)
            self.assertIn("1 rows named another company", said)

    def test_a_publication_before_the_trade_has_no_delay(self):
        def backwards(rows):
            next(r for r in rows if r["did"] == "570209")["acqtoDt"] = "10-Apr-2026"
        with tempfile.TemporaryDirectory() as tmp:
            saved = _copy_of_infy(Path(tmp), backwards)
            row = next(r for r in insider.insiders("INFY", 365, 0, TODAY, saved)
                       if r["id"] == "pit:570209")
            self.assertIsNone(row["days_to_publish"])


@unittest.skipUnless(has(PIT), "run capture.py")
class SharesAndLinksOnEveryRow(unittest.TestCase):
    def test_the_shares_and_the_holding_counts(self):
        rows = {r["id"]: r for r in insider.insiders("INFY", 365, 0, TODAY, EXCHANGE)}
        row = rows["pit:570209"]                   # 8,668,508 -> 8,650,911, 17,597 sold
        self.assertEqual((row["shares"], row["held_before"], row["held_after"]),
                         (17597, 8668508, 8650911))
        self.assertEqual(rows["pit:570210"]["held_before"], 0)     # Nil

    def test_the_table_shows_shares_and_every_row_its_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "2",
                              home=Path(tmp))
            self.assertIn(" shares ", said)
            self.assertIn("17597", said)
            self.assertIn("transaction 570209, filing 1197666, https://nsearchives", said)
            self.assertIn("195 more not shown (--top 2).", said)

    def test_deal_shares_are_whole_numbers_and_labels_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--bulk", "--today", "2026-10-02", "--top", "1", home=Path(tmp))
            self.assertIn("11535577 ", said)
            self.assertNotIn("11535577.0", said)
            _, said, _ = _run("--bulk", "--today", "2026-10-02", "--member", "irage",
                              home=Path(tmp))
            self.assertIn("both sides, flat by the close", said)


@unittest.skipUnless(has(PIT) and has(BULK), "run capture.py")
class RefusalsExitTwo(unittest.TestCase):
    """A refused question is not "nothing new": an alert loop reads exit 1 as quiet."""

    def test_each_is_refused_with_two(self):
        cases = [((), "Ask for at least one"),
                 (("--insiders", "--new"), "needs --symbol"),
                 (("--insiders", "--symbol", "../x"), "is not an NSE symbol"),
                 (("--insiders", "--symbol", "INFY", "--year", "0"), "--year wants"),
                 (("--insiders", "--symbol", "INFY", "--year", "10000"), "--year wants"),
                 (("--insiders", "--symbol", "INFY", "--days", "100000000"),
                  "before the calendar")]
        with tempfile.TemporaryDirectory() as tmp:
            for args, words in cases:
                code, said, _ = _run(*args, home=Path(tmp))
                self.assertEqual(code, 2, args)
                self.assertIn(words, said, args)

    def test_a_broken_state_file_is_refused_and_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for body in ("{bad", "[1, 2]", '{"seen": {"bulk_deals": []}}'):
                (home / "state.json").write_text(body, encoding="utf-8")
                code, said, _ = _run("--bulk", "--new", "--today", "2026-10-02", home=home)
                self.assertEqual(code, 2, body)
                self.assertIn("cannot be used", said)
                self.assertEqual((home / "state.json").read_text(encoding="utf-8"), body)

    def test_an_unreadable_portfolio_is_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "pf.json").write_text('{"positions": [1, "x"', encoding="utf-8")
            _, said, _ = _run("--bulk", "--today", "2026-10-02", "--top", "1",
                              "--portfolio", str(home / "pf.json"), home=home)
            self.assertIn("exists but could not be read", said)
            (home / "pf.json").write_text('{"positions": [1, "x"], "hits": "q"}',
                                          encoding="utf-8")
            self.assertEqual(insider.watch_from_files(home / "pf.json", home / "pf.json",
                                                      home / "pf.json"), [])

    def test_nothing_shown_exits_one_and_a_failure_with_it_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, said, _ = _run("--insiders", "--symbol", "INFY", home=Path(tmp))
            self.assertEqual(code, 1)                 # no INFY filing in the last 30 days
            self.assertIn("No filings for INFY", said)
            code, _, _ = _run("--insiders", "--tickers", "INFY", "TCS", home=Path(tmp))
            self.assertEqual(code, 2)                 # TCS unread: not "nothing"

    def test_a_ticker_named_twice_is_read_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--tickers", "INFY", "infy", "--symbol", "INFY",
                              "--days", "365", "--top", "1", home=Path(tmp))
            self.assertTrue(said.startswith("INFY insider and promoter filings"))
            self.assertIn("197 found", said)


@unittest.skipUnless(has(BULK), "run capture.py")
class WhatIsRememberedIsWhatWasShown(unittest.TestCase):
    def test_rows_cut_off_by_top_stay_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            for _ in range(3):
                _, said, _ = _run("--bulk", "--new", "--top", "30", "--today", "2026-10-02",
                                  "--json", home=Path(tmp))
                seen.append([r["id"] for r in json.loads(said)])
            self.assertEqual([len(s) for s in seen], [30, 30, 10])
            self.assertEqual(len(set(seen[0] + seen[1] + seen[2])), 70)

    def test_the_cut_is_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--bulk", "--new", "--top", "5", "--today", "2026-10-02",
                              home=Path(tmp))
            self.assertIn("65 more not shown (--top 5); they stay new", said)

    def test_a_forgotten_row_is_never_called_new_again(self):
        rows = insider.deals("bulk_deals", 30, None, 0, date(2026, 10, 2), EXCHANGE)
        state = {}
        insider.remember(rows[:1], state, "bulk_deals", date(2026, 10, 2),
                         insider.datetime(2026, 10, 2, 18, 0))
        self.assertEqual(len(insider.fresh(rows, state, "bulk_deals")), 69)
        # A year and more later the 24 September deals are forgotten: none of them is new.
        insider.remember([], state, "bulk_deals", date(2027, 11, 1),
                         insider.datetime(2027, 11, 1, 18, 0))
        self.assertEqual(state["seen"]["bulk_deals"], {})
        self.assertEqual(insider.fresh(rows, state, "bulk_deals"), [])

    def test_a_filter_that_does_not_apply_is_said_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--bulk", "--type", "ESOP", "--today", "2026-10-02", "--top", "1",
                              home=Path(tmp))
            self.assertIn("--type does not apply to deals", said)
            self.assertNotIn("match your filters", said)


@unittest.skipUnless(has(FLOWS), "run capture.py")
class FlowsInTheirWindow(unittest.TestCase):
    def test_a_flow_outside_the_window_is_not_shown_as_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, said, _ = _run("--flows", "--today", "2026-09-01", home=Path(tmp))
            self.assertEqual(code, 1)
            self.assertIn("for 2026-10-23, outside", said)
            self.assertNotIn("19232.31", said)

    def test_flows_alert_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _run("--flows", "--new", home=Path(tmp))
            second = _run("--flows", "--new", home=Path(tmp))
            self.assertEqual((first[0], second[0]), (0, 1))
            self.assertIn("for 2026-10-23", first[1])
            self.assertIn("Nothing new since", second[1])

    def test_top_limits_flows_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--flows", "--top", "1", "--json", home=Path(tmp))
            self.assertEqual(len(json.loads(said)), 1)


class TheSymbolIsSentWhole(unittest.TestCase):
    def test_an_ampersand_is_encoded_and_a_strange_answer_refused(self):
        asked = []
        real = insider.nse.fetch_json

        def fake(url, referer=None, timeout=30):
            asked.append(url)
            return {"acqNameList": []}
        insider.nse.fetch_json = fake
        try:
            with self.assertRaises(insider.NotPublished):
                insider.nse.insider_trades("M&M", 30, date(2026, 10, 24))
        finally:
            insider.nse.fetch_json = real
        self.assertTrue(asked[0].endswith("&symbol=M%26M"))



# ---------------------------------------------------------------- the third side-by-side pass

@unittest.skipUnless(has(PIT), "run capture.py")
class ASideThatCannotBeTrustedIsUnknown(unittest.TestCase):
    """The filing's type and its holding disagreeing, or a type that is not a buy or a sale, is
    not a buy or a sale. Picking one would put a false side on screen."""

    def row(self, change):
        with tempfile.TemporaryDirectory() as tmp:
            saved = _copy_of_infy(Path(tmp), change)
            return next(r for r in insider.insiders("INFY", 365, 0, TODAY, saved)
                        if r["id"] == "pit:570210")

    def test_the_type_and_the_holding_disagreeing_is_unknown(self):
        """570210 really went from Nil to 2,061 shares. Typed 'Sell', the two disagree."""
        def sold(rows):
            next(r for r in rows if r["did"] == "570210")["tdpTransactionType"] = "Sell"
        row = self.row(sold)
        self.assertEqual(row["side"], "unknown")
        self.assertIn("the filing says SELL but the holding went from 0 to 2061", row["side_from"])

    def test_a_type_that_is_not_a_trade_is_not_read_from_the_holding(self):
        def pledged(rows):
            next(r for r in rows if r["did"] == "570210")["tdpTransactionType"] = "Pledge Creation"
        row = self.row(pledged)
        self.assertEqual(row["side"], "unknown")
        self.assertIn("Pledge Creation", row["side_from"])

    def test_the_real_rows_are_unchanged(self):
        rows = insider.insiders("INFY", 365, 0, TODAY, EXCHANGE)
        self.assertEqual(sum(r["side"] == "unknown" for r in rows), 0)
        self.assertIn("where the two disagree", insider.INSIDER_NOTE)


@unittest.skipUnless(has(PIT), "run capture.py")
class ADerivativeCarriesItsNotional(unittest.TestCase):
    def test_the_notional_and_units_are_passed_through_as_filed(self):
        def options(rows):
            row = next(r for r in rows if r["did"] == "570209")
            row.update(derivativeType="Options", tdpDerivativeContractType="Call",
                       buyValue="15000000", buyQuantity="1200")
        with tempfile.TemporaryDirectory() as tmp:
            saved = _copy_of_infy(Path(tmp), options)
            rows = {r["id"]: r for r in insider.insiders("INFY", 365, 0, TODAY, saved)}
        row = rows["pit:570209"]
        self.assertEqual((row["derivative_buy_value"], row["derivative_buy_units"],
                          row["derivative_sell_value"], row["derivative_sell_units"]),
                         (15000000, 1200, 0, 0))
        # A share trade carries "0" in those fields in every real row: left out, not shown as 0.
        self.assertIsNone(rows["pit:570210"]["derivative_buy_value"])


@unittest.skipUnless(has(PIT), "run capture.py")
class SymbolsTheExchangeWouldNotKnow(unittest.TestCase):
    def test_a_yahoo_suffix_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, said, _ = _run("--insiders", "--symbol", "INFY.NS", home=Path(tmp))
            self.assertEqual(code, 2)
            self.assertIn("no exchange suffix such as .NS", said)

    def test_one_in_your_files_is_named_and_not_watched(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "pf.json").write_text(json.dumps({"positions": [
                {"symbol": "INFY.NS"}, {"tradingsymbol": "INFY"}]}), encoding="utf-8")
            odd = []
            self.assertEqual(insider.watch_from_files(home / "pf.json", home / "w.json",
                                                      home / "p.json", [], odd), ["INFY"])
            self.assertEqual(odd, ["INFY.NS"])
            code, said, _ = _run("--insiders", "--new", "--days", "365", "--top", "1",
                                 "--portfolio", str(home / "pf.json"), home=home)
            self.assertEqual(code, 0)
            self.assertIn("INFY.NS in your portfolio, plan or watchlist is not an NSE symbol", said)
            self.assertIn("INFY insider and promoter filings", said)

    def test_an_empty_answer_says_it_may_be_the_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", home=Path(tmp))
            self.assertIn("answers the same for a symbol it does not list", said)


@unittest.skipUnless(has(PIT) and has(BULK), "run capture.py")
class FiltersReadTheWayTheUsPackReadsThem(unittest.TestCase):
    def test_last_comma_first_finds_the_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--json",
                              "--member", "Majumdar, Mitrankur", home=Path(tmp))
            self.assertEqual(len(json.loads(said)), 3)

    def test_a_blank_filter_is_no_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            for flag in ("--member", "--type"):
                _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "1",
                                  flag, "", home=Path(tmp))
                self.assertIn("197 found.", said, flag)
                self.assertNotIn("match your filters", said, flag)

    def test_real_only_where_it_does_not_apply_is_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--insiders", "--symbol", "INFY", "--days", "365", "--top", "1",
                              "--real-only", home=Path(tmp))
            self.assertIn("--real-only does not apply to insider filings", said)
            _, said, _ = _run("--flows", "--real-only", home=Path(tmp))
            self.assertIn("--real-only does not apply to the market-wide flow", said)

    def test_a_negative_floor_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, said, _ = _run("--bulk", "--min-crore", "-1", home=Path(tmp))
            self.assertEqual(code, 2)
            self.assertIn("--min-crore wants 0 or more", said)

    def test_a_symbol_with_spaces_is_read_for_deals(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--block", "--symbol", " sanofi ", "--today", "2026-10-02",
                              "--json", home=Path(tmp))
            self.assertEqual(len(json.loads(said)), 2)


@unittest.skipUnless(has(BLOCK), "run capture.py")
class TwoLegsAlikeAreTwoDeals(unittest.TestCase):
    def test_each_has_its_own_id_and_the_second_stays_new(self):
        raw = fixture(BLOCK)
        sanofi = next(r for r in raw if r["BD_SYMBOL"] == "SANOFI" and r["BD_BUY_SELL"] == "SELL")
        raw.append(dict(sanofi))
        with tempfile.TemporaryDirectory() as tmp:
            saved = _folder(Path(tmp), {"block_deals.json": raw})
            rows = insider.deals("block_deals", 30, "SANOFI", 0, date(2026, 10, 2), saved)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len({r["id"] for r in rows}), 3)
            seen = []
            for _ in range(4):
                _, said, _ = _run("--block", "--symbol", "SANOFI", "--new", "--top", "1",
                                  "--today", "2026-10-02", "--json", home=Path(tmp), offline=saved)
                seen += [r["id"] for r in json.loads(said)]
            self.assertEqual(len(seen), 3)
            self.assertEqual(len(set(seen)), 3)

    def test_the_ids_of_the_real_deals_did_not_change(self):
        """A state file written before this change must still know what it has shown."""
        rows = insider.deals("block_deals", 30, "SANOFI", 0, date(2026, 10, 2), EXCHANGE)
        sell = next(r for r in rows if r["side"] == "SELL")
        key = "block_deals|2026-09-24|SANOFI|HOECHST GMBH|SELL|3500000.0|3052.0"
        self.assertEqual(sell["id"], "block_deals:" + insider.hashlib.sha1(key.encode())
                         .hexdigest()[:20])

    def test_a_block_leg_can_be_small(self):
        """The real SEDEMAC block deals include a leg of 0.09 crore, so the docstring must not
        call every block deal 10 crore or more."""
        rows = insider.deals("block_deals", 30, "SEDEMAC", 0, date(2026, 10, 2), EXCHANGE)
        self.assertLess(min(r["value_crore"] for r in rows), 1)
        self.assertNotIn("10 crore or more", insider.__doc__)


class ShortSellingIsReadOnlyAsJson(unittest.TestCase):
    def test_a_saved_csv_beside_it_is_not_read_as_bulk_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = _folder(Path(tmp), {"short_selling.json": [SHORT_ROW],
                                        "short_selling.csv": "SYMBOL,QTY\nADANIENSOL,1500\n"})
            code, said, _ = _run("--short", "--today", "2026-09-10", home=Path(tmp),
                                 offline=saved)
            self.assertEqual(code, 0)
            self.assertIn("ADANIENSOL", said)


@unittest.skipUnless(has(BULK) and has(PIT), "run capture.py")
class TheStateFileInDetail(unittest.TestCase):
    def test_a_date_that_is_not_text_is_refused_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for body in ('{"forgot_before": {"bulk_deals": 5}}',
                         '{"checked": {"bulk_deals": [1]}}', '{"last_check": 7}'):
                (home / "state.json").write_text(body, encoding="utf-8")
                code, said, _ = _run("--bulk", "--new", "--today", "2026-10-02", home=home)
                self.assertEqual(code, 2, body)
                self.assertIn("cannot be used", said)

    def test_it_is_written_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _run("--bulk", "--new", "--today", "2026-10-02", home=home)
            saved = json.loads((home / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["seen"]["bulk_deals"]), 30)
            self.assertEqual([p.name for p in home.iterdir()], ["state.json"])

    def test_a_saved_number_where_text_belongs_is_not_a_crash(self):
        raw = fixture(PIT)
        raw[0] = dict(raw[0], symbol=None, acqName=12345, acqMode=None)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "saved"
            folder.mkdir()
            (folder / "insider-INFY.json").write_text(json.dumps(raw), encoding="utf-8")
            rows = insider.insiders("INFY", 365, 0, TODAY, str(folder))
            row = next(r for r in rows if r["id"] == "pit:570210")
            self.assertEqual((row["who"], row["mode"], row["symbol"]), ("12345", "", "INFY"))


@unittest.skipUnless(has(BULK) and has(BLOCK), "run capture.py")
class InJsonEachLineSaysItsSection(unittest.TestCase):
    def test_the_cap_and_the_cut_name_their_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, err = _run("--bulk", "--block", "--today", "2026-10-02", "--top", "1",
                                "--json", home=Path(tmp))
            self.assertEqual(set(json.loads(said)), {"bulk_deals", "block_deals"})
            self.assertIn("Bulk deals: The exchange's JSON answer stops at 70", err)
            self.assertIn("Block deals: The exchange's JSON answer stops at 70", err)
            self.assertIn("Bulk deals: 69 more not shown", err)
            self.assertIn("Block deals: 69 more not shown", err)


@unittest.skipUnless(has(BLOCK) and has(FLOWS), "run capture.py")
class WhereADealIsListed(unittest.TestCase):
    """The US pack links every row to its filing. A deal has no page of its own, so the report
    page it is listed on is named."""

    def test_deals_and_flows_name_their_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, said, _ = _run("--block", "--today", "2026-10-02", "--top", "1", home=Path(tmp))
            self.assertIn("Listed at https://www.nseindia.com/report-detail/"
                          "display-bulk-and-block-deals", said)
            _, said, _ = _run("--flows", home=Path(tmp))
            self.assertIn("Listed at https://www.nseindia.com/reports/fii-dii", said)

    def test_an_empty_flow_answer_does_not_claim_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = _folder(Path(tmp), {"fii-dii.json": []})
            code, said, _ = _run("--flows", home=Path(tmp), offline=saved)
            self.assertEqual(code, 1)
            self.assertIn("held no flow rows", said)
            self.assertNotIn("not published", said)


if __name__ == "__main__":
    unittest.main(verbosity=2)
