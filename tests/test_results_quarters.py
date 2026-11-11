"""The quarterly results, read from what the companies filed with the exchange.

Every file here is a real XBRL filing or a real exchange answer, saved by capture.py. Where a guard
needs a case the market did not happen to supply, the test takes a real file and changes the one
fact the guard is about, so everything else in it is still what the company filed.
"""
import gzip
import json
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import FIXTURES, skill                                # noqa: E402
from nsetools import filings                                     # noqa: E402

results = skill("nse-results", "results")
SAVED = FIXTURES / "filings"
CRORE = 1e7


def quarters(symbol, count=6, offline=None):
    found, basis, problems, _unread = results.quarters_for(symbol, count, "consolidated",
                                                           str(offline or SAVED))
    return found, basis, problems


def analysed(symbol, count=6, offline=None):
    """The whole path one() takes: the quarters, the reasons any are missing, the actions."""
    found, _basis, _problems, unread = results.quarters_for(symbol, count, "consolidated",
                                                            str(offline or SAVED))
    return results.analyse(found, count, results.corporate_record(symbol, str(offline or SAVED)),
                           unread)


def copy_saved(tmp):
    shutil.copytree(SAVED, Path(tmp) / "f")
    return Path(tmp) / "f"


def bend(folder, filing, old, new):
    """Change one fact in a real saved filing, leaving everything else as the company filed it."""
    name = Path(folder) / (filing["xbrl"].rsplit("/", 1)[-1] + ".gz")
    text = gzip.decompress(name.read_bytes()).decode("utf-8")
    bent = re.sub(old, new, text, count=1, flags=re.S)
    assert bent != text, old
    name.write_bytes(gzip.compress(bent.encode("utf-8")))


def latest(symbol):
    rows = [r for r in filings.listing(symbol, str(SAVED)) if r["basis"] == "consolidated"]
    return filings.read(rows[0], str(SAVED))


def raw_of(row):
    return gzip.decompress((SAVED / (row["xbrl"].rsplit("/", 1)[-1] + ".gz")).read_bytes())


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class TwoListingsAreOneSeries(unittest.TestCase):
    def test_the_new_listing_and_the_old_one_meet_without_a_gap(self):
        ends = sorted({r["quarter_end"] for r in filings.listing("TCS", str(SAVED))})
        self.assertIn("2024-12-31", ends)          # the last quarter in the old listing
        self.assertIn("2025-03-31", ends)          # the first in the new one
        sources = {r["quarter_end"]: r["source"] for r in filings.listing("TCS", str(SAVED))}
        self.assertEqual(sources["2024-12-31"], "legacy")
        self.assertEqual(sources["2026-09-30"], "integrated")

    def test_one_row_per_quarter_and_basis(self):
        rows = filings.listing("HDFCBANK", str(SAVED))
        keys = [(r["quarter_end"], r["basis"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_newest_first(self):
        ends = [r["quarter_end"] for r in filings.listing("ITC", str(SAVED))]
        self.assertEqual(ends, sorted(ends, reverse=True))


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class WhatTheCompanyFiled(unittest.TestCase):
    def test_tcs_september_2026_quarter_as_filed(self):
        """The figures on the exchange's own page for the filing: 73,18,800 lakh of revenue,
        13,88,400 lakh to the owners, diluted EPS 38.37."""
        q = latest("TCS")
        self.assertEqual((q["start"], q["end"]), ("2026-07-01", "2026-09-30"))
        self.assertEqual((q["kind"], q["basis"], q["currency"]), ("company", "consolidated", "INR"))
        self.assertEqual(q["values"]["revenue"], 731_880_000_000)
        self.assertEqual(q["values"]["profit_owners"], 138_840_000_000)
        self.assertEqual(q["values"]["eps_diluted"], 38.37)

    def test_the_quarter_is_read_by_its_context_not_the_year_to_date(self):
        """The same file carries six months to 30 September (FourD); only OneD is the quarter."""
        q = latest("TCS")
        self.assertEqual(q["year_to_date"]["start"], "2026-04-01")
        self.assertEqual(q["year_to_date"]["values"]["revenue"], 1_454_630_000_000)
        self.assertNotEqual(q["values"]["revenue"], q["year_to_date"]["values"]["revenue"])

    def test_a_bank_is_read_with_the_banking_names(self):
        q = latest("HDFCBANK")
        self.assertEqual(q["kind"], "bank")
        text = raw_of(q["filing"]).decode("utf-8")
        income = re.search(r'<in-capmkt:Income contextRef="OneD"[^>]*>([^<]+)<', text).group(1)
        self.assertEqual(q["values"]["revenue"], float(income))
        self.assertIn("operating_profit", q["values"])

    def test_names_match_exactly_never_by_prefix(self):
        """ProfitLossForPeriod is a prefix of ProfitLossForPeriodFromContinuingOperations."""
        for symbol in ("TCS", "ITC"):
            for row in [r for r in filings.listing(symbol, str(SAVED))
                        if r["basis"] == "consolidated"][:10]:
                text = raw_of(row).decode("utf-8")
                exact = re.search(r'<in-capmkt:ProfitLossForPeriod contextRef="OneD"[^>]*>([^<]+)<',
                                  text)
                if exact:
                    q = filings.read(row, str(SAVED))
                    self.assertEqual(q["values"]["profit"], float(exact.group(1)), row)

    def test_a_quarter_context_that_spans_more_than_a_quarter_is_refused(self):
        """The real TCS filing with its OneD start moved back to April: six months is not a
        quarter, and reading it as one stores half a year as three months."""
        row = latest("TCS")["filing"]
        text = raw_of(row).decode("utf-8")
        bent = re.sub(r'(<xbrli:context id="OneD">.*?<xbrli:startDate>)2026-07-01',
                      r"\g<1>2026-04-01", text, count=1, flags=re.S)
        self.assertNotEqual(bent, text)
        with self.assertRaises(filings.NotFiled):
            filings.parse(bent.encode("utf-8"))

    def test_a_listing_that_names_the_wrong_quarter_is_refused(self):
        row = dict(latest("TCS")["filing"], quarter_end="2026-06-30")
        with self.assertRaises(filings.NotFiled):
            filings.read(row, str(SAVED))


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class TheQuartersAddUpToTheYear(unittest.TestCase):
    def by_end(self, symbol):
        return {q["end"]: q for q in quarters(symbol)[0]}

    def test_tcs_fy2026_adds_up_exactly(self):
        q = self.by_end("TCS")
        ends = ("2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31")
        total = sum(q[e]["values"]["revenue"] for e in ends)
        self.assertEqual(total, q["2026-03-31"]["year_to_date"]["values"]["revenue"])
        self.assertEqual(total, 2_670_210_000_000)
        self.assertNotIn("FY26", results.restated_years(q))

    def test_itc_fy2025_does_not_add_up_and_is_flagged(self):
        flagged = results.restated_years(self.by_end("ITC"))
        self.assertIn("FY25", flagged)
        self.assertIn("83,037.81", flagged["FY25"])
        self.assertIn("81,612.78", flagged["FY25"])

    def test_a_growth_figure_leaning_on_that_year_says_so(self):
        found = results.analyse(quarters("ITC")[0], 6, None)
        row = next(r for r in found["rows"] if r["end"] == "2025-06-30")
        self.assertTrue(row["restated_base"])
        self.assertTrue(any("FY25" in n for n in row["notes"]))
        clean = next(r for r in found["rows"] if r["end"] == "2026-06-30")
        self.assertFalse(clean["restated_base"])

    def test_the_flag_states_the_numbers_and_no_reason(self):
        """Why a year disagrees is not in the filings, so no reason may be printed."""
        for text in results.restated_years(self.by_end("HDFCBANK")).values():
            for guess in ("restated", "demerger", "merger", "because"):
                self.assertNotIn(guess, text)


@unittest.skipUnless((SAVED / "HDFCBANK-actions.json").exists(), "run capture.py")
class EarningsPerShareAcrossABonus(unittest.TestCase):
    """HDFC Bank's 1:1 bonus went ex on 26 August 2025. Its June 2025 EPS was filed as 21.12, on
    the shares before it; a year later it filed 12.47. Read straight, that is -41%."""

    @classmethod
    def setUpClass(cls):
        corporate = results.corporate_record("HDFCBANK", str(SAVED))
        cls.found = results.analyse(quarters("HDFCBANK")[0], 6, corporate)
        cls.rows = {r["end"]: r for r in cls.found["rows"]}

    def test_growth_across_the_bonus_is_withheld_with_the_reason(self):
        row = self.rows["2026-06-30"]
        self.assertEqual(row["eps_diluted"], 12.47)
        self.assertIsNone(row["eps_yoy"])
        self.assertTrue(any("Bonus 1:1" in n for n in row["notes"]))

    def test_growth_with_no_bonus_between_is_given(self):
        """September 2025 and September 2026 are both after the bonus."""
        self.assertEqual(self.rows["2026-09-30"]["eps_yoy"], 5.5)

    def test_profit_growth_is_not_touched_by_a_bonus(self):
        self.assertIsNotNone(self.rows["2026-06-30"]["profit_yoy"])

    def test_with_no_record_of_actions_eps_growth_is_refused_not_assumed(self):
        found = results.analyse(quarters("HDFCBANK")[0], 6, None)
        self.assertTrue(all(r["eps_yoy"] is None for r in found["rows"]))

    def test_the_last_four_quarters_add_up(self):
        t = self.found["ttm"]
        by_end = {q["end"]: q for q in quarters("HDFCBANK")[0]}
        ends = sorted(by_end, reverse=True)[:4]
        self.assertEqual(t["profit_owners"], sum(by_end[e]["values"]["profit_owners"] for e in ends))
        self.assertEqual(t["eps_diluted"],
                         round(sum(by_end[e]["values"]["eps_diluted"] for e in ends), 2))


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class TheMargins(unittest.TestCase):
    def test_tcs_operating_profit_is_computed_from_filed_lines(self):
        """186,380 before exceptional items and tax, plus 2,520 finance costs, less 13,370 other
        income: 175,530 crore of operating profit on 731,880 of revenue is 24.0%."""
        row = results.analyse(quarters("TCS")[0], 1, None)["rows"][0]
        self.assertEqual(row["operating_profit"], 175_530_000_000)
        self.assertFalse(row["operating_profit_filed"])
        self.assertEqual(row["operating_margin"], 24.0)

    def test_a_company_that_files_no_cost_of_goods_has_no_gross_margin(self):
        row = results.analyse(quarters("TCS")[0], 1, None)["rows"][0]
        self.assertIsNone(row["gross_margin"])

    def test_a_company_that_does_has_one(self):
        row = results.analyse(quarters("ITC")[0], 1, None)["rows"][0]
        self.assertIsNotNone(row["gross_margin"])

    def test_a_bank_has_no_gross_margin_and_its_operating_profit_is_filed(self):
        row = results.analyse(quarters("HDFCBANK")[0], 1, None)["rows"][0]
        self.assertIsNone(row["gross_margin"])
        self.assertTrue(row["operating_profit_filed"])

    def test_the_quarters_are_named_for_indias_year(self):
        self.assertEqual(results.fiscal("2026-09-30"), "Q2 FY27")
        self.assertEqual(results.fiscal("2026-03-31"), "Q4 FY26")
        self.assertEqual(results.fiscal("2025-12-31"), "Q3 FY26")

    def test_one_basis_for_every_quarter(self):
        found, basis, _ = quarters("HDFCBANK")
        self.assertEqual({q["basis"] for q in found}, {basis})


@unittest.skipUnless((SAVED / "RELIANCE-integrated.json").exists(), "run capture.py")
class ABaseThatWasLargeOutsideTheBusiness(unittest.TestCase):
    """Reliance's June 2025 quarter carried 15,119 crore of other income; the June 2026 quarter
    6,550. Its owners' profit reads -22.4% a year on, against that base."""

    @classmethod
    def setUpClass(cls):
        corporate = results.corporate_record("RELIANCE", str(SAVED))
        cls.rows = {r["end"]: r for r in results.analyse(quarters("RELIANCE")[0], 6,
                                                         corporate)["rows"]}

    def test_the_fall_is_shown_as_filed(self):
        self.assertEqual(self.rows["2026-06-30"]["profit_yoy"], -22.4)

    def test_and_the_base_is_described_with_its_figures(self):
        notes = self.rows["2026-06-30"]["notes"]
        self.assertTrue(any("15,119" in n and "6,550" in n for n in notes), notes)

    def test_without_giving_a_reason_the_filings_do_not_carry(self):
        for note in self.rows["2026-06-30"]["notes"]:
            for guess in ("because", "stake", "sale", "gain"):
                self.assertNotRegex(note, rf"{guess}")

    def test_an_ordinary_base_gets_no_note(self):
        self.assertFalse(any("other income" in n for n in self.rows["2026-03-31"]["notes"]))


@unittest.skipUnless((SAVED / "ITC-board-meetings.json").exists(), "run capture.py")
class TheNextReportAndALaggingFiling(unittest.TestCase):
    def test_the_next_results_meeting_comes_from_the_exchanges_notice(self):
        meetings = filings.board_meetings("ITC", str(SAVED))
        said = results.next_results(meetings, date(2026, 10, 24))
        self.assertIn("2026-11-02", said)

    def test_none_announced_says_so_and_does_not_guess(self):
        meetings = filings.board_meetings("TCS", str(SAVED))
        said = results.next_results(meetings, date(2026, 10, 24))
        self.assertIn("No results board meeting has been announced", said)

    def test_a_meeting_held_with_no_xbrl_yet_is_a_note(self):
        """The day after ITC's 2 November meeting the saved listing has no September quarter."""
        found = results.one("ITC", 6, "consolidated", str(SAVED), False, date(2026, 11, 3))
        self.assertIsNotNone(found["stale"])
        self.assertIn("2026-11-02", found["stale"])

    def test_before_the_meeting_there_is_no_note(self):
        found = results.one("ITC", 6, "consolidated", str(SAVED), False, date(2026, 10, 24))
        self.assertIsNone(found["stale"])


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class TheCommand(unittest.TestCase):
    def test_an_offline_run_works_and_exits_zero(self):
        self.assertEqual(results.main(["TCS", "--offline", str(SAVED), "--now",
                                       "2026-10-24 09:00"]), 0)

    def test_a_symbol_with_nothing_saved_exits_two(self):
        self.assertEqual(results.main(["NOSUCH", "--offline", str(SAVED)]), 2)

    def test_metrics_merge_a_row_per_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "metrics.json"
            for symbol in ("TCS", "ITC", "TCS"):
                results.main([symbol, "--offline", str(SAVED), "--now", "2026-10-24 09:00",
                              "--metrics", str(out)])
            saved = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual([c["symbol"] for c in saved["companies"]], ["ITC", "TCS"])
            tcs = saved["companies"][1]
            self.assertEqual(tcs["period_end"], "2026-09-30")
            self.assertEqual(tcs["revenue_ttm"], 2_832_480_000_000)

    def test_a_filing_in_another_currency_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(SAVED, Path(tmp) / "f")
            row = latest("TCS")["filing"]
            name = Path(tmp) / "f" / (row["xbrl"].rsplit("/", 1)[-1] + ".gz")
            text = gzip.decompress(name.read_bytes()).decode("utf-8")
            name.write_bytes(gzip.compress(text.replace(
                '<in-capmkt:DescriptionOfPresentationCurrency contextRef="OneD">INR',
                '<in-capmkt:DescriptionOfPresentationCurrency contextRef="OneD">USD').encode()))
            with self.assertRaises(filings.NotFiled):
                results.one("TCS", 6, "consolidated", str(Path(tmp) / "f"), True,
                            date(2026, 10, 24))

    def test_the_watchlist_supplies_the_symbols(self):
        path = FIXTURES / "chain" / "watchlist.json"
        body = json.loads(path.read_text(encoding="utf-8"))["data"]
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "watchlist.json"
            plain.write_text(json.dumps(body), encoding="utf-8")
            self.assertEqual(results.watchlist_symbols(plain), ["SBIN", "SUZLON", "ITC"])


@unittest.skipUnless((SAVED / "HDFCBANK-legacy.json.gz").exists(), "run capture.py")
class AYearCheckThatCannotBeMadeIsSaid(unittest.TestCase):
    """HDFC Bank's FY2025 quarters add to 2,198 crore more owners' profit than the year. Whether
    that is caught must not depend on how many quarters were asked for, or on the older listing
    answering."""

    def test_the_flag_does_not_depend_on_how_many_quarters_are_shown(self):
        for count in (3, 6):
            row = next(r for r in analysed("HDFCBANK", count)["rows"] if r["end"] == "2026-03-31")
            self.assertTrue(row["restated_base"], count)
            self.assertTrue(any("70,342.56" in n for n in row["notes"]), count)

    def test_without_the_older_listing_the_check_says_it_could_not_be_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            (folder / "HDFCBANK-legacy.json.gz").unlink()
            found = results.one("HDFCBANK", 6, "consolidated", str(folder), True,
                                date(2026, 10, 24))
        self.assertTrue(any("older results listing" in p for p in found["problems"]))
        row = next(r for r in found["result"]["rows"] if r["end"] == "2026-03-31")
        self.assertFalse(row["restated_base"])
        self.assertEqual(row["restated_unchecked"], ["FY25"])
        said = [n for n in row["notes"] if n.startswith("restated check not possible")]
        self.assertTrue(said and "older results listing" in said[0], row["notes"])
        self.assertTrue(any("Not read: the older results listing" in line
                            for line in results.render(found, "consolidated")))

    def test_a_year_whose_filing_does_not_carry_the_whole_year_is_not_tested(self):
        """TCS's real March 2026 filing with its year to date moved to start in October, as a
        company with an October year files it: four quarters against six months would print a
        false disagreement, so the year is reported as unchecked instead."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            march = next(r for r in filings.listing("TCS", str(folder))
                         if r["quarter_end"] == "2026-03-31" and r["basis"] == "consolidated")
            bend(folder, march,
                 r'(<in-capmkt:DateOfStartOfReportingPeriod contextRef="FourD"[^>]*>)2025-04-01',
                 r"\g<1>2025-10-01")
            checks = results.year_checks({q["end"]: q for q in quarters("TCS", 6, folder)[0]})
        self.assertEqual(checks["FY26"][0], "unchecked")
        self.assertIn("whole of FY26", checks["FY26"][1])


@unittest.skipUnless((SAVED / "HDFCBANK-actions.json").exists(), "run capture.py")
class TheShareBasisIsFixedWhenTheResultsArePublished(unittest.TestCase):
    """HDFC Bank's real September 2025 and September 2024 quarters, with the real 1:1 bonus moved
    to go ex on 5 October 2025: after the quarter ended, before its results were published on 18
    October. Ind AS 33 restates that quarter's EPS for it, so growth must be withheld."""

    def test_a_bonus_after_the_quarter_but_before_publication_withholds_eps_growth(self):
        corporate = [dict(a) for a in results.corporate_record("HDFCBANK", str(SAVED))]
        for a in corporate:
            if "Bonus" in (a.get("subject") or ""):
                a["exDate"] = "05-Oct-2025"
        found = quarters("HDFCBANK")[0]
        row = next(r for r in results.analyse(found, 6, corporate)["rows"]
                   if r["end"] == "2025-09-30")
        self.assertIsNone(row["eps_yoy"])
        self.assertTrue(any("2025-10-05" in n for n in row["notes"]), row["notes"])

    def test_the_last_four_quarters_close_at_publication_too(self):
        corporate = [dict(a) for a in results.corporate_record("HDFCBANK", str(SAVED))]
        corporate.append({"exDate": "10-Oct-2026", "subject": "Bonus 1:1"})   # filed 17 Oct
        result = results.analyse(quarters("HDFCBANK")[0], 6, corporate)
        self.assertNotIn("eps_diluted", result["ttm"])
        self.assertTrue(result["ttm_notes"])


@unittest.skipUnless((SAVED / "ITC-integrated.json").exists(), "run capture.py")
class DiscontinuedOperations(unittest.TestCase):
    """ITC's March 2025 quarter carried 14,652.61 crore of profit from discontinued operations
    (the hotels it demerged), inside 19,727 crore of owners' profit."""

    @classmethod
    def setUpClass(cls):
        cls.result = analysed("ITC")
        cls.rows = {r["end"]: r for r in cls.result["rows"]}

    def test_it_is_read_as_filed(self):
        self.assertEqual(self.rows["2025-03-31"]["discontinued"], 146_526_100_000)

    def test_a_base_that_carried_it_is_stated_with_its_figures(self):
        notes = self.rows["2026-03-31"]["notes"]
        self.assertTrue(any("discontinued operations" in n and "14,652.61" in n for n in notes),
                        notes)

    def test_without_giving_a_reason(self):
        for note in self.rows["2026-03-31"]["notes"]:
            for guess in ("because", "hotel", "demerg", "sale", "gain"):
                self.assertNotIn(guess, note.lower())

    def test_the_summary_says_it_when_the_newest_quarter_compares_with_it(self):
        lines = results.changes({"rows": [r for r in self.result["rows"]
                                          if r["end"] <= "2026-03-31"]})
        self.assertTrue(any("discontinued operations" in s and "14,652.61" in s for s in lines))


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class TheCompanysOwnFinancialYear(unittest.TestCase):
    def test_the_quarter_is_named_from_the_year_the_filing_states(self):
        q = latest("TCS")
        self.assertEqual((q["year_start"], q["year_end"], q["fiscal"]),
                         ("2026-04-01", "2027-03-31", "Q2 FY27"))

    def test_an_october_year_names_september_the_fourth_quarter(self):
        """The real TCS filing with its financial year restated as October to September."""
        text = raw_of(latest("TCS")["filing"]).decode("utf-8")
        for name, old, new in (("DateOfStartOfFinancialYear", "2026-04-01", "2025-10-01"),
                               ("DateOfEndOfFinancialYear", "2027-03-31", "2026-09-30")):
            text = re.sub(rf'(<in-capmkt:{name} contextRef="OneD"[^>]*>){old}', rf"\g<1>{new}",
                          text, count=1)
        self.assertEqual(filings.parse(text.encode("utf-8"))["fiscal"], "Q4 FY26")

    def test_a_quarter_off_the_companys_year_is_refused_plainly(self):
        text = raw_of(latest("TCS")["filing"]).decode("utf-8")
        text = re.sub(r'(<in-capmkt:DateOfStartOfFinancialYear contextRef="OneD"[^>]*>)2026-04-01',
                      r"\g<1>2026-05-01", text, count=1)
        with self.assertRaises(filings.NotFiled) as caught:
            filings.parse(text.encode("utf-8"))
        self.assertIn("not a quarter of the company's financial year", str(caught.exception))

    def test_a_filing_that_states_no_currency_is_refused(self):
        text = raw_of(latest("TCS")["filing"]).decode("utf-8")
        text = re.sub(r'<in-capmkt:DescriptionOfPresentationCurrency contextRef="OneD"[^>]*>INR'
                      r'</in-capmkt:DescriptionOfPresentationCurrency>', "", text, count=1)
        with self.assertRaises(filings.NotFiled) as caught:
            filings.parse(text.encode("utf-8"))
        self.assertEqual(str(caught.exception), "currency not stated")

    def test_a_february_year_end_has_a_year_ago(self):
        self.assertEqual(results.year_ago("2024-02-29"), "2023-02-28")


@unittest.skipUnless((SAVED / "TCS-board-meetings.json").exists(), "run capture.py")
class SmallerThings(unittest.TestCase):
    def test_a_meeting_that_already_produced_the_newest_filing_is_not_next(self):
        """TCS met on 8 October 2026 and its filing went up that evening."""
        meetings = filings.board_meetings("TCS", str(SAVED))
        self.assertTrue(any(m["date"] == "2026-10-08" for m in meetings))
        said = results.next_results(meetings, date(2026, 10, 8), "2026-10-08T22:07:33")
        self.assertIn("No results board meeting has been announced", said)

    def test_a_meeting_today_with_no_filing_yet_is_still_next(self):
        meetings = filings.board_meetings("TCS", str(SAVED))
        said = results.next_results(meetings, date(2026, 10, 8), "2026-07-09T18:00:00")
        self.assertIn("2026-10-08", said)

    def test_bad_settings_are_refused_plainly(self):
        with tempfile.TemporaryDirectory() as tmp:
            for setting in ({"results_basis": "both"}, {"results_quarters": "six"},
                            {"results_quarters": 0}):
                conf = Path(tmp) / "config.json"
                conf.write_text(json.dumps(setting), encoding="utf-8")
                self.assertEqual(results.main(["TCS", "--offline", str(SAVED), "--config",
                                               str(conf)]), 2, setting)

    def test_a_withheld_four_quarter_eps_says_why_in_the_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            # The real record, with one bonus added: ex 10 October 2026, inside the newest
            # quarter's publication window (it was published on 17 October).
            path = folder / "HDFCBANK-actions.json"
            body = json.loads(path.read_text(encoding="utf-8"))
            body["data"].append({"exDate": "10-Oct-2026", "subject": "Bonus 1:1"})
            path.write_text(json.dumps(body), encoding="utf-8")
            out = Path(tmp) / "metrics.json"
            results.main(["HDFCBANK", "--offline", str(folder), "--now", "2026-10-24 09:00",
                          "--metrics", str(out), "--json"])
            row = json.loads(out.read_text(encoding="utf-8"))["companies"][0]
        self.assertIsNone(row["eps_ttm"])
        self.assertTrue(any("withheld" in n for n in row["ttm_notes"]), row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
