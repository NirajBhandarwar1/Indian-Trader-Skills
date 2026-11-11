"""The gaps a side-by-side check against the US earnings reader found, each pinned by a test.

Every input is a real saved filing or a real saved exchange answer. Where a guard needs a case the
market did not supply, the test copies a real file and changes the one fact the guard is about.
"""
import contextlib
import gzip
import io
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

from _load import FIXTURES, has, raw, skill                      # noqa: E402
from nsetools import actions, filings, nse, shares, yahoo        # noqa: E402

results = skill("nse-results", "results")
SAVED = FIXTURES / "filings"
NOW = ["--now", "2026-10-24 09:00"]
ACCOUNTS = {s: f"accounts/{s}.json" for s in ("RELIANCE", "TCS")}


def copy_saved(tmp):
    shutil.copytree(SAVED, Path(tmp) / "f")
    return Path(tmp) / "f"


def row_for(symbol, end, folder=SAVED):
    return next(r for r in filings.listing(symbol, str(folder))
                if r["quarter_end"] == end and r["basis"] == "consolidated")


def file_of(folder, row):
    return Path(folder) / (row["xbrl"].rsplit("/", 1)[-1] + ".gz")


def bend(folder, row, old, new):
    """Change one fact in a real saved filing, leaving everything else as the company filed it."""
    name = file_of(folder, row)
    text = gzip.decompress(name.read_bytes()).decode("utf-8")
    bent = re.sub(old, new, text, count=1, flags=re.S)
    assert bent != text, old
    name.write_bytes(gzip.compress(bent.encode("utf-8")))


def analysed(symbol, folder=SAVED, count=6, corporate="saved"):
    found, _b, problems, unread = results.quarters_for(symbol, count, "consolidated", str(folder))
    if corporate == "saved":
        corporate = results.corporate_record(symbol, str(folder))
    return results.analyse(found, count, corporate, unread), problems


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = results.main(argv)
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class PrintedAsTheUSPackPrintsIt(unittest.TestCase):
    def test_an_exact_zero_growth_is_plain_zero(self):
        """ITC's real December 2025 EPS (3.94) against December 2024 (3.94): 0.0%, no sign."""
        self.assertEqual(results._p(0.0), "0.0%")
        self.assertEqual(results._p(-0.0), "0.0%")
        self.assertEqual(results._p(2.5), "+2.5%")
        code, out, _ = run(["ITC", "--offline", str(SAVED), *NOW])
        line = next(s for s in out.splitlines() if s.startswith("2025-12-31"))
        self.assertEqual(code, 0)
        self.assertIn(" 0.0%", line)
        self.assertNotIn("+0.0%", line)

    def test_the_header_names_the_source(self):
        _code, out, _ = run(["TCS", "--offline", str(SAVED), *NOW])
        self.assertIn("Source: NSE results filings (XBRL)", out.splitlines()[0])

    def test_a_missing_figure_in_the_summary_reads_n_a(self):
        result, _ = analysed("TCS")
        result["rows"][0]["profit_owners"] = None
        self.assertIn("owners' profit n/a", results.changes(result)[0])


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class TheQuarterBeforeIsTheQuarterBefore(unittest.TestCase):
    def test_a_missing_quarter_is_not_compared_with_under_that_name(self):
        """TCS's real filings with the June 2026 one gone: the next row is March 2026, two
        quarters back, and "vs the quarter before" would be false about it."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            file_of(folder, row_for("TCS", "2026-06-30")).unlink()
            result, problems = analysed("TCS", folder)
        self.assertEqual(result["rows"][1]["end"], "2026-03-31")
        said = " ".join(results.changes(result))
        self.assertNotIn("quarter before", said)
        self.assertTrue(any(p.startswith("2026-06-30") for p in problems))

    def test_with_every_quarter_there_it_is_compared(self):
        result, _ = analysed("TCS")
        self.assertIn("quarter before", " ".join(results.changes(result)))


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class AFilingThatCannotBeReadIsRefused(unittest.TestCase):
    def test_a_figure_given_as_nan_refuses_the_quarter(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            bend(folder, row_for("TCS", "2026-09-30"),
                 r'(<in-capmkt:RevenueFromOperations contextRef="OneD"[^>]*>)[^<]+', r"\g<1>NaN")
            result, problems = analysed("TCS", folder)
        self.assertNotEqual(result["rows"][0]["end"], "2026-09-30")
        self.assertTrue(any("2026-09-30" in p and "not a figure" in p for p in problems), problems)

    def test_a_filing_with_none_of_the_names_read_is_refused_not_shown_blank(self):
        """The real TCS filing with its revenue and profit lines renamed, as a statement format
        this does not read would carry them: a row of blanks would look like a quarter."""
        text = gzip.decompress(file_of(SAVED, row_for("TCS", "2026-09-30")).read_bytes())
        text = text.decode("utf-8")
        for name in ("RevenueFromOperations", "ProfitLossForPeriod",
                     "ProfitOrLossAttributableToOwnersOfParent"):
            text = re.sub(rf"(</?in-capmkt:){name}(?=[\s>])", rf"\g<1>Other{name}", text)
        with self.assertRaises(filings.NotFiled) as caught:
            filings.parse(text.encode("utf-8"))
        self.assertIn("no revenue and no profit", str(caught.exception))

    def test_a_file_on_the_other_basis_is_not_mixed_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            row = row_for("TCS", "2026-09-30")
            bend(folder, row, r'(NatureOfReportStandaloneConsolidated contextRef="OneD"[^>]*>)'
                              r'Consolidated', r"\g<1>Standalone")
            with self.assertRaises(filings.NotFiled) as caught:
                filings.read(row, str(folder))
        self.assertIn("standalone", str(caught.exception))

    def test_a_damaged_saved_listing_is_a_plain_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            path = folder / "TCS-integrated.json"
            path.write_bytes(path.read_bytes()[:500])
            code, _out, err = run(["TCS", "--offline", str(folder), *NOW])
        self.assertEqual(code, 2)
        self.assertIn("damaged", err)

    def test_a_damaged_saved_filing_is_listed_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            name = file_of(folder, row_for("TCS", "2026-09-30"))
            name.write_bytes(name.read_bytes()[:300])
            _result, problems = analysed("TCS", folder)
        self.assertTrue(any("2026-09-30" in p and "damaged" in p for p in problems), problems)

    def test_junk_rows_in_a_listing_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            path = folder / "TCS-integrated.json"
            body = json.loads(path.read_text(encoding="utf-8"))
            listing = body["data"]
            inner = listing["data"] if isinstance(listing, dict) else listing
            inner += ["junk", 7, None, {"qe_Date": 5, "xbrl": 9}]
            path.write_text(json.dumps(body), encoding="utf-8")
            rows = filings.listing("TCS", str(folder))
        self.assertEqual(rows, filings.listing("TCS", str(SAVED)))


@unittest.skipUnless((SAVED / "TCS-actions.json").exists(), "run capture.py")
class TheShareBasisFailsSafe(unittest.TestCase):
    def test_a_bonus_whose_ex_date_cannot_be_read_withholds_eps_growth(self):
        """TCS's real record, which clears its EPS growth, with one bonus added whose ex-date is
        unreadable: it might fall inside the window, so the growth cannot be shown."""
        clean, _ = analysed("TCS")
        self.assertIsNotNone(clean["rows"][0]["eps_yoy"])
        corporate = results.corporate_record("TCS", str(SAVED)) + [
            {"exDate": "-", "subject": "Bonus 1:1"}]
        result, _ = analysed("TCS", corporate=corporate)
        self.assertIsNone(result["rows"][0]["eps_yoy"])
        self.assertTrue(any("cannot be read" in n for n in result["rows"][0]["notes"]))
        self.assertNotIn("eps_diluted", result["ttm"])

    def test_a_dividend_with_an_unreadable_date_changes_nothing(self):
        corporate = results.corporate_record("TCS", str(SAVED)) + [
            {"exDate": "-", "subject": "Interim Dividend - Rs 10 Per Share"}]
        result, _ = analysed("TCS", corporate=corporate)
        self.assertIsNotNone(result["rows"][0]["eps_yoy"])

    def test_a_damaged_saved_record_is_unchecked_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            path = folder / "TCS-actions.json"
            path.write_text(path.read_text(encoding="utf-8")[:200], encoding="utf-8")
            self.assertIsNone(results.corporate_record("TCS", str(folder)))
            result, _ = analysed("TCS", folder)
        self.assertIsNone(result["rows"][0]["eps_yoy"])


@unittest.skipUnless((SAVED / "HDFCBANK-integrated.json").exists(), "run capture.py")
class ProfitAboveTheOperatingLine(unittest.TestCase):
    def test_a_bank_is_flagged_too(self):
        """HDFC Bank's real September 2026 filing with its operating profit before provisions
        set below its owners' profit: the US pack flags this for every company, banks too."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            bend(folder, row_for("HDFCBANK", "2026-09-30"),
                 r'(<in-capmkt:OperatingProfitBeforeProvisionAndContingencies contextRef="OneD"'
                 r'[^>]*>)[^<]+', r"\g<1>100000000000")
            result, _ = analysed("HDFCBANK", folder)
        said = " ".join(results.changes(result))
        self.assertIn("above operating profit before provisions", said)

    def test_the_sentence_names_lines_and_gives_no_reason(self):
        """ITC's real March 2025 quarter: owners' profit far above the operating line, from its
        discontinued operations. The sentence lists what can sit between, not a cause."""
        result, _ = analysed("ITC")
        rows = [r for r in result["rows"] if r["end"] <= "2025-03-31"]
        said = " ".join(results.changes({"rows": rows}))
        self.assertIn("discontinued operations", said)
        for guess in ("because", "hotel", "demerg"):
            self.assertNotIn(guess, said.lower())


@unittest.skipUnless((SAVED / "ITC-board-meetings.json").exists(), "run capture.py")
class TheNoticesAroundTheResults(unittest.TestCase):
    def test_a_meeting_today_is_next_not_already_held(self):
        """ITC's real 2 November 2026 meeting, on the day itself: the notice gives no hour, so
        "the board met" would be false that morning. It is the next meeting instead."""
        found = results.one("ITC", 6, "consolidated", str(SAVED), False, date(2026, 11, 2))
        self.assertIsNone(found["stale"])
        self.assertIn("2026-11-02", found["next_results"])

    def test_notices_that_cannot_be_read_are_said_and_the_results_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            path = folder / "ITC-board-meetings.json"
            path.write_text(path.read_text(encoding="utf-8")[:100], encoding="utf-8")
            found = results.one("ITC", 6, "consolidated", str(folder), False, date(2026, 10, 24))
        self.assertTrue(found["result"]["rows"])
        self.assertIn("could not be read", found["next_results"])
        self.assertNotIn("has been announced yet", found["next_results"])
        self.assertTrue(any(p.startswith("board-meeting notices") for p in found["problems"]))

    def test_damaged_announcements_are_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            path = folder / "TCS-announcements.json"
            path.write_text(path.read_text(encoding="utf-8")[:100], encoding="utf-8")
            found = results.one("TCS", 6, "consolidated", str(folder), False, date(2026, 10, 24))
        self.assertEqual(found["documents"], [])
        self.assertTrue(any(p.startswith("results-day announcements") for p in found["problems"]))

    def test_a_notice_with_no_timestamp_says_nothing_about_when_it_was_given(self):
        meetings = [dict(m, told=None) for m in filings.board_meetings("ITC", str(SAVED))]
        said = results.next_results(meetings, date(2026, 10, 24))
        self.assertIn("2026-11-02", said)
        self.assertNotIn("told the exchange", said)


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class TheLastFourQuarters(unittest.TestCase):
    def test_a_quarter_with_no_eps_filed_leaves_the_total_out_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            bend(folder, row_for("TCS", "2026-06-30"),
                 r'<in-capmkt:DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations '
                 r'contextRef="OneD"[^>]*>[^<]+</in-capmkt:DilutedEarningsLossPerShareFromContinuing'
                 r'AndDiscontinuedOperations>', "")
            found = results.one("TCS", 6, "consolidated", str(folder), True, date(2026, 10, 24))
        ttm = found["result"]["ttm"]
        self.assertNotIn("eps_diluted", ttm)
        self.assertIn("revenue", ttm)
        notes = found["result"]["ttm_notes"]
        self.assertTrue(any("not given" in n and "2026-06-30" in n for n in notes), notes)
        line = next(s for s in results.render(found, "consolidated")
                    if s.startswith("Last 4 quarters"))
        self.assertTrue(line.endswith("diluted EPS -."), line)


@unittest.skipUnless((SAVED / "ITC-integrated.json").exists(), "run capture.py")
class ADiscontinuedLineNotFiled(unittest.TestCase):
    def test_is_said_as_not_filed_never_as_zero(self):
        result, _ = analysed("ITC")
        now = next(r for r in result["rows"] if r["end"] == "2026-03-31")
        base = {"discontinued": 146_526_100_000.0}
        notes = results.base_notes({"other_income": now["other_income"]}, base, "2026-03-31")
        said = next(n for n in notes if "discontinued" in n)
        self.assertIn("none filed this quarter", said)
        self.assertNotIn("Rs 0 cr", said)


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class TheMetricsFile(unittest.TestCase):
    def test_a_file_that_is_not_a_metrics_file_is_refused_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "metrics.json"
            for body in ("not json at all", json.dumps([1, 2]),
                         json.dumps({"companies": [{"ticker": "AAPL"}]})):
                out.write_text(body, encoding="utf-8")
                code, _o, err = run(["TCS", "--offline", str(SAVED), *NOW, "--metrics", str(out)])
                self.assertEqual(code, 2, body)
                self.assertIn("nothing was written", err)
                self.assertEqual(out.read_text(encoding="utf-8"), body)

    def test_the_example_has_every_field_a_run_writes(self):
        example = json.loads((Path(results.__file__).parent / "metrics.example.json")
                             .read_text(encoding="utf-8"))
        found = results.one("TCS", 6, "consolidated", str(SAVED), True, date(2026, 10, 24))
        written = results.metrics_row(found)
        for company in example["companies"]:
            self.assertEqual(set(company), set(written), company["symbol"])
        tcs = next(c for c in example["companies"] if c["symbol"] == "TCS")
        self.assertEqual(tcs, written)

    def test_a_watchlist_of_the_wrong_shape_gives_no_symbols_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.json"
            for body in ('"TCS"', "5", '{"hits": "TCS"}', '[5, null, {"symbol": 7}, " itc "]'):
                path.write_text(body, encoding="utf-8")
                got = results.watchlist_symbols(path)
                self.assertEqual(got, ["ITC"] if "itc" in body else [], body)


class Accounts:
    """The real saved keyless responses, handed to the real parser by the symbol asked for."""

    def __enter__(self):
        self.real = yahoo._fetch
        yahoo._fetch = lambda url, timeout=30: raw(
            ACCOUNTS[re.search(r"/timeseries/(\w+)\.NS", url).group(1)])
        return self

    def __exit__(self, *_):
        yahoo._fetch = self.real


@unittest.skipUnless(all(has(p) for p in ACCOUNTS.values()), "run capture.py")
class FourYears(unittest.TestCase):
    def test_offline_is_refused_rather_than_going_to_the_network(self):
        code, _o, err = run(["TCS", "--annual", "--offline", str(SAVED)])
        self.assertEqual(code, 2)
        self.assertIn("--offline", err)

    def test_json_is_one_document_for_every_company_with_its_warnings(self):
        with Accounts():
            code, out, _ = run(["RELIANCE", "TCS", "--annual", "--json"])
        self.assertEqual(code, 0)
        docs = json.loads(out)
        self.assertEqual([d["symbol"] for d in docs], ["RELIANCE", "TCS"])
        self.assertIn("cannot_all_be_right", docs[0])
        self.assertEqual(docs[0]["years"][0]["revenue"], 1057219)

    def test_save_keeps_each_company_under_its_own_name(self):
        with tempfile.TemporaryDirectory() as tmp, Accounts():
            real = results.show.shared
            results.show.shared = lambda name: Path(tmp) / name
            try:
                code, out, _ = run(["RELIANCE", "TCS", "--annual", "--save"])
            finally:
                results.show.shared = real
            saved = json.loads((Path(tmp) / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(saved["companies"]), ["RELIANCE", "TCS"])
        self.assertIn("2026-03-31", saved["companies"]["RELIANCE"])

    def test_a_year_in_another_currency_is_not_shown_in_crore(self):
        with Accounts():
            by_year = yahoo.accounts("RELIANCE")
        by_year["2026-03-31"]["currency"] = "USD"
        row = next(r for r in results.table_rows(by_year) if r["year_end"] == "2026-03-31")
        self.assertIsNone(row["revenue"])
        self.assertIsNone(row["profit"])
        other = next(r for r in results.table_rows(by_year) if r["year_end"] == "2025-03-31")
        self.assertIsNotNone(other["revenue"])

    def test_ratio_edges(self):
        with Accounts():
            real = yahoo.accounts("RELIANCE")["2026-03-31"]
        v = real["values"]
        zero = results.ratios({"y": {"currency": "INR", "values": dict(v, net_profit_owners=0.0)}})
        self.assertEqual(zero[0]["roe_percent"], 0.0)
        negative = results.ratios({"y": {"currency": "INR", "values": dict(
            v, net_profit_owners=-abs(v["net_profit_owners"]), equity=-abs(v["equity"]))}})
        self.assertIsNone(negative[0]["roe_percent"])
        self.assertIsNone(negative[0]["debt_to_equity"])
        no_group = {k: x for k, x in v.items() if k != "net_profit"}
        self.assertIsNone(results.ratios({"y": {"currency": "INR",
                                                "values": no_group}})[0]["roa_percent"])

    def test_a_figure_given_as_nan_is_left_blank(self):
        body = raw(ACCOUNTS["RELIANCE"]).decode("utf-8")
        bent = re.sub(r'("annualTotalRevenue":\s*\[.*?"raw":\s*)[-\d.eE+]+', r"\g<1>NaN", body,
                      count=1, flags=re.S)
        self.assertNotEqual(bent, body)
        real = yahoo._fetch
        yahoo._fetch = lambda url, timeout=30: bent.encode("utf-8")
        try:
            by_year = yahoo.accounts("RELIANCE")
        finally:
            yahoo._fetch = real
        for year in by_year.values():
            for value in year["values"].values():
                self.assertEqual(value, value)        # no nan anywhere


class ASourceThatCouldNotBeReached(unittest.TestCase):
    """"Filed none" and "could not be read" are different statements about a company."""

    YEARS = ["2026-03-31"]

    def setUp(self):
        self.filed, self.counts = shares.as_filed, yahoo.unconfirmed_share_counts
        self.actions = nse.corporate_actions

    def tearDown(self):
        shares.as_filed, yahoo.unconfirmed_share_counts = self.filed, self.counts
        nse.corporate_actions = self.actions

    def test_an_unreachable_exchange_is_not_said_to_have_filed_nothing(self):
        def refuse(*a, **k):
            raise OSError("no network")
        shares.as_filed = refuse
        yahoo.unconfirmed_share_counts = lambda s, **k: {"2026-03-31": 13532472634.0}
        _counts, missing = shares.confirmed("TESTCO", self.YEARS)
        self.assertIn("could not be read", missing["2026-03-31"])
        self.assertNotIn("filed no", missing["2026-03-31"])

    def test_an_unreachable_accounts_source_is_not_said_to_report_nothing(self):
        def refuse(*a, **k):
            raise yahoo.Unreachable("could not reach the source")
        yahoo.unconfirmed_share_counts = refuse
        shares.as_filed = lambda s, w: {"2026-03-31": 13532472634}
        counts, missing = shares.confirmed("TESTCO", self.YEARS)
        self.assertEqual(counts, {})
        self.assertIn("could not be read", missing["2026-03-31"])
        self.assertNotIn("only the exchange reports", missing["2026-03-31"])

    def test_the_real_unreachable_answer_is_raised_not_read_as_none(self):
        """With the network blocked, the real fetcher's refusal must not become 'no count'."""
        yahoo.unconfirmed_share_counts = self.counts
        with self.assertRaises(yahoo.Unreachable):
            yahoo.unconfirmed_share_counts("TCS")

    def test_a_listed_filing_that_could_not_be_fetched_is_said(self):
        got = shares.Counts()
        got.unreadable["2026-03-31"] = "its shareholding filing could not be fetched (x)"
        shares.as_filed = lambda s, w: got
        yahoo.unconfirmed_share_counts = lambda s, **k: {"2026-03-31": 13532472634.0}
        _counts, missing = shares.confirmed("TESTCO", self.YEARS)
        self.assertIn("could not be fetched", missing["2026-03-31"])

    def test_an_unread_action_record_explains_nothing(self):
        def refuse(*a, **k):
            raise nse.NotPublished("the exchange answered 403")
        nse.corporate_actions = refuse
        real = shares.confirmed
        shares.confirmed = lambda s, w: ({}, {"2026-03-31": "the two sources do not report the "
                                                            "same count"})
        try:
            row = results.share_count_rows("TESTCO", {"2026-03-31": {"currency": "INR",
                                                                      "values": {}}})[0]
        finally:
            shares.confirmed = real
        self.assertIn("could not be read", row["refused"][0])
        self.assertNotIn("No bonus or split", row["refused"][0])


class ASymbolWithAnAmpersand(unittest.TestCase):
    """M&M is a real NSE symbol. Unescaped, "symbol=M&M&..." asks the exchange about "M", and
    an empty corporate-action record for it reads as "no bonus or split"."""

    def test_every_request_this_skill_makes_escapes_it(self):
        asked = []

        def record(url, *a, **k):
            asked.append(url)
            return []
        saved = (filings.fetch_json, nse.fetch_json, yahoo._fetch)
        filings.fetch_json = nse.fetch_json = record
        yahoo._fetch = lambda url, timeout=30: asked.append(url) or b"{}"
        try:
            filings.listing("M&M")
            filings.board_meetings("M&M")
            nse.corporate_actions("M&M")
            nse.announcements("M&M")
            nse.shareholding("M&M")
            with self.assertRaises(yahoo.NotReported):
                yahoo.accounts("M&M")
        finally:
            filings.fetch_json, nse.fetch_json, yahoo._fetch = saved
        self.assertEqual(len(asked), 7)
        for url in asked:
            self.assertIn("M%26M", url)
            self.assertNotIn("M&M", url)


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class SmallerGaps(unittest.TestCase):
    def test_an_older_listing_of_the_wrong_shape_is_said(self):
        saved = filings.fetch_json
        bodies = iter([json.loads((SAVED / "TCS-integrated.json").read_text(encoding="utf-8"))
                       ["data"], {"error": "x"}])
        filings.fetch_json = lambda url, *a, **k: next(bodies)
        problems = []
        try:
            rows = filings.listing("TCS", None, problems)
        finally:
            filings.fetch_json = saved
        self.assertTrue(rows)
        self.assertTrue(any("shape" in p for p in problems), problems)

    def test_no_publication_day_gives_no_lag_note_rather_than_a_false_one(self):
        result, _ = analysed("ITC")
        result["rows"][0]["filed"] = None
        meetings = filings.board_meetings("ITC", str(SAVED))
        self.assertIsNone(results.stale_note(result, meetings, date(2026, 11, 3)))

    def test_a_refusal_names_the_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            path = folder / "TCS-integrated.json"
            path.write_bytes(path.read_bytes()[:500])
            _code, _out, err = run(["TCS", "--offline", str(folder), *NOW])
        self.assertTrue(err.startswith("results: TCS"), err)


class HostileActionRecords(unittest.TestCase):
    def test_odd_types_in_a_record_do_not_crash(self):
        self.assertIsNone(actions.parse_day(20241028))
        self.assertIsNone(actions.factor(5))
        self.assertIsNotNone(results.basis_moved(["junk"], "2025-06-30", "2026-07-01"))


def real_text(symbol, end):
    return gzip.decompress(file_of(SAVED, row_for(symbol, end)).read_bytes()).decode("utf-8")


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class NoFigureIsGuessedFromAGap(unittest.TestCase):
    """A line that is not filed, or not a number, is never read as a zero."""

    def test_a_figure_that_is_not_a_number_refuses_the_quarter(self):
        """TCS's real September 2026 filing with its finance costs written "1,234": read as
        blank, operating profit would be off by the whole line in silence."""
        text = re.sub(r'(<in-capmkt:FinanceCosts contextRef="OneD"[^>]*>)[^<]+', r"\g<1>1,234",
                      real_text("TCS", "2026-09-30"), count=1)
        with self.assertRaises(filings.NotFiled) as caught:
            filings.parse(text.encode("utf-8"))
        self.assertIn("not a figure", str(caught.exception))

    def test_a_stated_start_that_disagrees_with_the_quarter_refuses_it(self):
        text = re.sub(r'(<in-capmkt:DateOfStartOfReportingPeriod contextRef="OneD"[^>]*>)[^<]+',
                      r"\g<1>2026-04-01", real_text("TCS", "2026-09-30"), count=1)
        with self.assertRaises(filings.NotFiled) as caught:
            filings.parse(text.encode("utf-8"))
        self.assertIn("starts 2026-04-01", str(caught.exception))

    def test_operating_profit_is_not_computed_without_its_finance_costs(self):
        q = filings.parse(real_text("TCS", "2026-09-30").encode("utf-8"))
        self.assertEqual(results.operating_profit(q)[0], 175530000000.0)   # 24.0% of 73,188 cr
        text = re.sub(r'<in-capmkt:FinanceCosts contextRef="OneD"[^>]*>[^<]+</in-capmkt:FinanceCosts>',
                      "", real_text("TCS", "2026-09-30"), count=1)
        q = filings.parse(text.encode("utf-8"))
        self.assertNotIn("finance_costs", q["values"])
        self.assertIsNone(results.operating_profit(q)[0])

    def test_gross_profit_is_not_computed_with_one_cost_line_missing(self):
        """ITC's real June 2026 filing carries all three; with purchases gone, a gross margin
        would be revenue less two of the three, too high by the purchases."""
        q = filings.parse(real_text("ITC", "2026-06-30").encode("utf-8"))
        self.assertIsNotNone(results.gross_profit(q))
        text = re.sub(r'<in-capmkt:PurchasesOfStockInTrade contextRef="OneD"[^>]*>[^<]+'
                      r'</in-capmkt:PurchasesOfStockInTrade>', "", real_text("ITC", "2026-06-30"),
                      count=1)
        q = filings.parse(text.encode("utf-8"))
        self.assertNotIn("purchases", q["values"])
        self.assertIsNone(results.gross_profit(q))

    def test_a_small_figure_is_not_printed_as_zero(self):
        self.assertEqual(results.money(-40000.0), "Rs -0.004 cr")
        self.assertEqual(results.money(0.0), "Rs 0 cr")
        self.assertEqual(results.money(-7531000000.0), "Rs -753.1 cr")


@unittest.skipUnless((SAVED / "TCS-integrated.json").exists(), "run capture.py")
class AYearCheckReadsWhatItNeeds(unittest.TestCase):
    def test_an_unreadable_quarter_does_not_turn_a_listed_one_into_not_listed(self):
        """TCS's real filings with June 2026 gone and two quarters shown: the March 2026 row leans
        on FY25, whose June 2024 quarter is listed and saved but lay past the first read. It was
        reported as "not listed on this basis by the exchange", which was false."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = copy_saved(tmp)
            file_of(folder, row_for("TCS", "2026-06-30")).unlink()
            result, problems = analysed("TCS", folder, count=2)
        march = next(r for r in result["rows"] if r["end"] == "2026-03-31")
        self.assertEqual(march["restated_unchecked"], [])
        self.assertFalse(any("not listed" in n for n in march["notes"]), march["notes"])
        self.assertTrue(any(p.startswith("2026-06-30") for p in problems))


class ARefusalIsAnError(unittest.TestCase):
    def test_bad_settings_are_said_on_stderr_leaving_json_output_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            for setting in ({"results_basis": "both"}, {"results_quarters": "six"},
                            {"results_quarters": 0}):
                conf = Path(tmp) / "config.json"
                conf.write_text(json.dumps(setting), encoding="utf-8")
                code, out, err = run(["TCS", "--offline", str(SAVED), "--json",
                                      "--config", str(conf)])
                self.assertEqual((code, out), (2, ""), setting)
                self.assertIn("Refused:", err)
        code, out, err = run(["TCS", "--offline", str(SAVED), "--now", "whenever"])
        self.assertEqual((code, out), (2, ""))


@unittest.skipUnless((SAVED / "TCS-board-meetings.json").exists(), "run capture.py")
class AnUnreadableNewestFilingIsNotAMissingOne(unittest.TestCase):
    """TCS's real September 2026 filing, published 8 October, damaged so it cannot be read."""

    def damaged(self, tmp):
        folder = copy_saved(tmp)
        bend(folder, row_for("TCS", "2026-09-30"),
             r'(<in-capmkt:RevenueFromOperations contextRef="OneD"[^>]*>)[^<]+', r"\g<1>NaN")
        return folder

    def test_the_note_does_not_say_the_exchange_has_no_xbrl(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(["TCS", "--offline", str(self.damaged(tmp)), *NOW])
        self.assertEqual(code, 0)
        note = next(line for line in out.splitlines() if line.startswith("NOTE:"))
        self.assertNotIn("not on the exchange yet", note)
        self.assertIn("could not be read here", note)
        self.assertIn("2026-10-08", note)

    def test_a_meeting_that_has_reported_is_not_next(self):
        """The evening of 8 October: the filing is out (if unreadable), so 8 October is not
        "next" and a trader is not sent looking for results that are already published."""
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(["TCS", "--offline", str(self.damaged(tmp)),
                                "--now", "2026-10-08 23:00"])
        self.assertEqual(code, 0)
        self.assertNotIn("Next results board meeting: 2026-10-08", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
