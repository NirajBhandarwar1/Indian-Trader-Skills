"""The share basis and the share count: what makes a figure publishable.

EVERY RATIO AND EVERY COUNT HERE IS REAL. The corporate actions are the exchange's own records for
RELIANCE, TCS, HDFCBANK and SBIN; the share counts are parsed out of the exchange's own
shareholding XBRL documents; and the wording cases come from a census of 773 share-affecting
actions filed market-wide since 2022.
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import fixture, has, raw, skill                       # noqa: E402
from nsetools import actions, nse, shares                        # noqa: E402

results = skill("nse-results", "results")

ACTIONS = {s: f"exchange/actions-{s}.json" for s in
           ("RELIANCE", "TCS", "HDFCBANK", "SBIN")}
XBRL = {"TCS": "exchange/shareholding-TCS-2026-09-30.xml.gz",
        "RELIANCE": "exchange/shareholding-RELIANCE-2026-09-30.xml.gz"}


def actions_of(symbol: str):
    return fixture(ACTIONS[symbol])


def subjects(symbol: str):
    return [(r.get("subject") or "").strip() for r in actions_of(symbol)]


@unittest.skipUnless(all(has(p) for p in ACTIONS.values()), "run capture.py")
class ExactFactorsOnRealActions(unittest.TestCase):
    """A bonus and a split restate per-share figures by an amount that is exactly knowable."""

    def test_reliances_real_one_for_one_bonus_doubles_the_count(self):
        """Ex 2024-10-28, in the exchange's own record."""
        bonus = [r for r in actions_of("RELIANCE") if "bonus" in (r.get("subject") or "").lower()]
        self.assertTrue(bonus)
        for row in bonus:
            self.assertEqual(actions.factor(row["subject"]), 2.0)
        latest = max(actions.parse_day(r["exDate"]) for r in bonus)
        self.assertEqual(latest, date(2024, 10, 28))

    def test_every_dividend_in_the_real_record_changes_no_basis(self):
        for symbol in ACTIONS:
            for subject in subjects(symbol):
                if "dividend" in subject.lower() and ":" not in subject:
                    self.assertIsNone(actions.factor(subject), subject)

    def test_a_real_demerger_changes_no_basis(self):
        """Reliance's 2023 demerger handed out shares in the company split off.

        The parent's own count did not change, so per-share history is untouched.
        """
        demergers = [s for s in subjects("RELIANCE") if "demerger" in s.lower()]
        self.assertTrue(demergers)
        for subject in demergers:
            self.assertIsNone(actions.factor(subject))

    def test_a_real_rights_issue_is_uncertain(self):
        """Reliance's 1:15 rights issue of 2020 restates only its bonus element.

        That depends on the price and the take-up, so no free source pins it down exactly.
        """
        rights = [s for s in subjects("RELIANCE") if "rights" in s.lower()]
        self.assertTrue(rights)
        for subject in rights:
            with self.assertRaises(actions.Uncertain):
                actions.factor(subject)

    def test_every_real_subject_either_parses_or_refuses_and_never_guesses(self):
        """No subject in four companies' full records may come back with a wrong factor."""
        for symbol in ACTIONS:
            for subject in subjects(symbol):
                try:
                    got = actions.factor(subject)
                except actions.Uncertain:
                    continue
                if got is not None:
                    self.assertGreater(got, 1.0, subject)
                    self.assertLess(got, 100.0, subject)


class TheExchangesWording(unittest.TestCase):
    """Taken verbatim from the market-wide census, which is why these spellings are here."""

    def test_the_bonus_ratios_the_market_really_files(self):
        for subject, expected in (("Bonus 1:1", 2.0), ("Bonus 1:2", 1.5), ("Bonus 2:1", 3.0),
                                  ("Bonus 1:10", 1.1), ("Bonus 2:5", 1.4),
                                  ("Bonus- 1:2", 1.5)):
            self.assertAlmostEqual(actions.factor(subject), expected, places=6, msg=subject)

    def test_the_face_value_splits_the_market_really_files(self):
        self.assertEqual(actions.factor(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"), 10.0)
        self.assertEqual(actions.factor(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"), 5.0)
        self.assertEqual(actions.factor(
            "Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share"), 2.0)

    def test_the_spelling_with_no_space_after_rs(self):
        """'Rs10/-' appears twice in the real census and must not be missed."""
        self.assertEqual(actions.factor(
            "Face Value Split (Sub-Division) - From Rs10/- Per Share To Rs 5/- Per Share"), 2.0)

    def test_a_capital_reduction_is_uncertain(self):
        with self.assertRaises(actions.Uncertain):
            actions.factor("Capital Reduction Pursuant To Nclt Order")

    def test_a_bonus_of_preference_shares_is_not_read_as_an_equity_bonus(self):
        """'Scheme Of Arrangement - Bonus Ncrps 4:1' is real, and reading it as equity would
        multiply every per-share figure by five."""
        with self.assertRaises(actions.Uncertain):
            actions.factor("Scheme Of Arrangement - Bonus Ncrps 4:1")

    def test_an_unrecognised_subject_carrying_a_ratio_is_uncertain(self):
        """Ratios turn up in corporate actions exactly when a holding is being restated.

        Ignoring one would miss an event in silence, which is the dangerous failure.
        """
        with self.assertRaises(actions.Uncertain):
            actions.factor("Some New Thing The Exchange Invented 3:7")

    def test_a_buyback_does_not_refuse_the_year(self):
        """REGRESSION. A buyback changes the count but restates nothing.

        Treating it as a basis change refused TCS FY2023, whose figures are exactly as filed.
        """
        self.assertIsNone(actions.factor("Buy Back"))


@unittest.skipUnless(has(ACTIONS["RELIANCE"]), "run capture.py")
class TheWindowThatMatters(unittest.TestCase):
    """Only events AFTER a year end restate that year's per-share figures."""

    def test_reliances_bonus_restates_the_years_that_closed_before_it(self):
        rows = actions_of("RELIANCE")
        for year_end in ("2023-03-31", "2024-03-31"):
            factor, why = actions.changed_since(rows, year_end)
            self.assertEqual(factor, 2.0, year_end)
            self.assertTrue(any("2024-10-28" in line for line in why))

    def test_it_leaves_alone_the_years_that_closed_after_it(self):
        for year_end in ("2025-03-31", "2026-03-31"):
            factor, why = actions.changed_since(actions_of("RELIANCE"), year_end)
            self.assertEqual(factor, 1.0, year_end)
            self.assertEqual(why, [])

    def test_a_company_with_no_basis_change_in_the_window(self):
        """SBIN filed no bonus or split in the captured record, so nothing is restated."""
        factor, why = actions.changed_since(actions_of("SBIN"), "2023-03-31")
        self.assertEqual((factor, why), (1.0, []))

    def test_a_bonus_with_an_unreadable_ex_date_fails_closed(self):
        """It could fall inside the window, so the window cannot be cleared: refused, not skipped.

        Reliance's real record with its real bonus's ex-date made unreadable. Skipped, the 2023
        year would read as unrestated (1.0x) when the exchange's own record says it was doubled.
        """
        rows = [dict(r) for r in actions_of("RELIANCE")]
        bonus = next(r for r in rows if "bonus" in (r.get("subject") or "").lower())
        bonus["exDate"] = "-"
        with self.assertRaises(actions.Uncertain) as caught:
            actions.changed_since(rows, "2023-03-31")
        self.assertIn("ex-date that cannot be read", str(caught.exception))
        with self.assertRaises(actions.Uncertain):
            actions.basis_change(rows, date(2025, 3, 31), date(2025, 6, 30))

    def test_a_split_or_an_unreadable_action_with_no_date_fails_closed_too(self):
        for subject in ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- "
                        "Per Share", "Rights 1:15 @ Premium Rs 1247/-"):
            with self.assertRaises(actions.Uncertain, msg=subject):
                actions.changed_since([{"exDate": "", "subject": subject}], "2020-03-31")

    def test_a_dividend_with_an_unreadable_ex_date_changes_nothing(self):
        """A dividend moves no share count, so its date does not matter."""
        rows = [dict(r) for r in actions_of("RELIANCE")
                if "dividend" in (r.get("subject") or "").lower()]
        self.assertTrue(rows)
        rows[0]["exDate"] = "-"
        self.assertEqual(actions.changed_since(rows, "2025-03-31"), (1.0, []))

    def test_an_entry_that_is_not_a_record_fails_closed(self):
        with self.assertRaises(actions.Uncertain):
            actions.changed_since(actions_of("SBIN") + ["junk"], "2023-03-31")

    def test_dates_are_compared_as_dates(self):
        self.assertEqual(actions.parse_day("28-Oct-2024"), date(2024, 10, 28))
        self.assertIsNone(actions.parse_day("-"))


@unittest.skipUnless(all(has(p) for p in XBRL.values()), "run capture.py")
class TheExchangesOwnShareCount(unittest.TestCase):
    """Parsed out of the real shareholding XBRL, which is the free second source."""

    def total(self, symbol: str) -> int:
        body = raw(XBRL[symbol]).decode("utf-8", "replace")
        found = [int(v) for v in shares.TOTAL.findall(body)]
        self.assertTrue(found, f"no share count parsed from {symbol}")
        return max(found)

    def test_a_real_filing_yields_a_whole_share_count(self):
        for symbol in XBRL:
            self.assertGreater(self.total(symbol), 10 ** 8, symbol)

    def test_tcs_matches_the_accounts_source_to_the_share(self):
        """TCS holds no stock in trust for itself, so the two sources agree exactly.

        This is the case where a count can be published.
        """
        self.assertEqual(self.total("TCS"), 3618087518)

    def test_reliance_does_not_match_and_that_is_not_an_error(self):
        """Reliance holds its own stock through trusts, so issued and held differ.

        Neither source is wrong; they count different things. The count is withheld.
        """
        self.assertEqual(self.total("RELIANCE"), 13312748854)
        self.assertFalse(shares.agree(13532472634.0, self.total("RELIANCE")))

    def test_the_parser_survives_both_taxonomy_versions(self):
        """The context holding the total was RENAMED between the 2022 and 2025 schemas.

        A parser keyed on either name returns nothing for half the filings, which reads as "the
        exchange has no share count". Taking the largest value cannot fail that way.
        """
        source = (Path(__file__).resolve().parent.parent
                  / "nsetools" / "shares.py").read_text(encoding="utf-8")
        self.assertIn("RENAMED", source)
        for symbol in XBRL:
            body = raw(XBRL[symbol]).decode("utf-8", "replace")
            self.assertIn("NumberOfFullyPaidUpEquityShares", body)

    def test_the_largest_value_really_is_the_grand_total_context(self):
        """This is what makes taking the maximum correct rather than merely convenient.

        The categories nest -- public contains its own subsets -- so the second and third largest
        values overlap and cannot be added. What holds is that the largest value is the one filed
        against the whole-pattern context, whatever that context is called in this taxonomy.
        """
        import re
        pairs = re.compile(r"<[\w.-]+:NumberOfFullyPaidUpEquityShares\s+contextRef=\"([^\"]+)\""
                           r"[^>]*>\s*(\d+)\s*<")
        for symbol in XBRL:
            body = raw(XBRL[symbol]).decode("utf-8", "replace")
            found = [(int(v), cx) for cx, v in pairs.findall(body)]
            self.assertTrue(found, symbol)
            biggest, context = max(found)
            self.assertIn("ShareholdingPattern", context, f"{symbol}: max sat on {context}")
            self.assertEqual(biggest, max(int(v) for v in shares.TOTAL.findall(body)))


@unittest.skipUnless(has("exchange/shareholding-TCS-filings.json"), "run capture.py")
class TheFilingsList(unittest.TestCase):
    def test_dates_are_read_into_iso_order(self):
        self.assertEqual(shares._iso("31-MAR-2023"), "2023-03-31")
        self.assertEqual(shares._iso("30-SEP-2026"), "2026-09-30")
        self.assertIsNone(shares._iso("whenever"))

    def test_the_latest_filing_is_found_by_date_not_by_text(self):
        """Sorted as text, 31-MAR beats 30-SEP, so 'latest' comes out eighteen months stale."""
        real = fixture("exchange/shareholding-TCS-filings.json")
        iso = sorted({shares._iso(r.get("date") or "") for r in real} - {None})
        self.assertEqual(iso[-1], "2026-09-30")
        as_text = sorted((r.get("date") or "") for r in real)
        self.assertNotEqual(shares._iso(as_text[-1]), iso[-1])

    def test_every_real_filing_carries_an_xbrl_link(self):
        for row in fixture("exchange/shareholding-TCS-filings.json"):
            self.assertTrue((row.get("xbrl") or "").startswith("http"))


class TwoSourcesOnTheShareCount(unittest.TestCase):
    """There is no tolerance. A count counts things, so two sources match or they do not."""

    def test_identical_counts_agree(self):
        self.assertTrue(shares.agree(3618087518.0, 3618087518))

    def test_a_two_hundredth_of_a_percent_is_still_a_different_number(self):
        """ITC's two counts sit 0.02% apart, which looks like rounding and is 3,104,065 shares."""
        self.assertFalse(shares.agree(12529468231.0, 12526364166))

    def test_one_share_apart_is_a_different_number(self):
        self.assertFalse(shares.agree(3618087518.0, 3618087517))

    def test_a_missing_second_source_never_agrees(self):
        self.assertFalse(shares.agree(3618087518.0, None))
        self.assertFalse(shares.agree(None, 3618087518))

    def test_a_fractional_count_is_never_agreed(self):
        self.assertFalse(shares.agree(3618087518.5, 3618087518))


@unittest.skipUnless(has(ACTIONS["RELIANCE"]), "run capture.py")
class OnlyProvedCountsBecomeData(unittest.TestCase):
    """confirmed() is the only door a share count comes through, and it is exact."""

    YEARS = ["2026-03-31"]

    def setUp(self):
        self.filed, self.counts = shares.as_filed, shares.yahoo.unconfirmed_share_counts
        shares.yahoo.unconfirmed_share_counts = lambda s, **k: {"2026-03-31": 13532472634.0}

    def tearDown(self):
        shares.as_filed = self.filed
        shares.yahoo.unconfirmed_share_counts = self.counts

    def test_an_identical_count_becomes_data(self):
        shares.as_filed = lambda s, w: {"2026-03-31": 13532472634}
        counts, missing = shares.confirmed("TESTCO", self.YEARS)
        self.assertEqual(counts, {"2026-03-31": 13532472634})
        self.assertEqual(missing, {})

    def test_the_real_reliance_gap_never_becomes_data(self):
        shares.as_filed = lambda s, w: {"2026-03-31": 13291184806}
        counts, missing = shares.confirmed("TESTCO", self.YEARS)
        self.assertEqual(counts, {})
        self.assertIn("do not report the same count", missing["2026-03-31"])

    def test_neither_rival_figure_appears_in_the_reason(self):
        """A second source decides whether a number can be shown; it never becomes a number."""
        shares.as_filed = lambda s, w: {"2026-03-31": 13291184806}
        _, missing = shares.confirmed("TESTCO", self.YEARS)
        for figure in ("13532472634", "13,532,472,634", "13291184806", "13,291,184,806"):
            self.assertNotIn(figure, missing["2026-03-31"])

    def test_one_source_alone_never_becomes_data(self):
        shares.as_filed = lambda s, w: {}
        counts, missing = shares.confirmed("TESTCO", self.YEARS)
        self.assertEqual(counts, {})
        self.assertIn("nothing to confirm it", missing["2026-03-31"])

    def test_an_unreachable_exchange_yields_no_count(self):
        def refuse(*a, **k):
            raise OSError("no network")
        shares.as_filed = refuse
        counts, missing = shares.confirmed("TESTCO", self.YEARS)
        self.assertEqual(counts, {})
        self.assertIn("2026-03-31", missing)


@unittest.skipUnless(has(ACTIONS["RELIANCE"]), "run capture.py")
class TheRowsSayWhyNot(unittest.TestCase):
    """A blank carries its reason, so a missing figure is never a silent one."""

    YEAR = {"2026-03-31": {"currency": "INR", "values": {"net_profit_owners": 807750000000.0}}}

    def setUp(self):
        self.confirmed, self.actions = shares.confirmed, nse.corporate_actions
        nse.corporate_actions = lambda *a, **k: []

    def tearDown(self):
        shares.confirmed, nse.corporate_actions = self.confirmed, self.actions

    def test_a_proved_count_is_shown(self):
        shares.confirmed = lambda s, w: ({"2026-03-31": 13532472634}, {})
        row = results.share_count_rows("TESTCO", self.YEAR)[0]
        self.assertEqual(row["shares"], 13532472634)
        self.assertEqual(row["refused"], [])

    def test_a_disagreement_is_explained_by_the_real_bonus_that_caused_it(self):
        """Fed Reliance's own corporate action record, the reason names the real bonus."""
        shares.confirmed = lambda s, w: ({}, {"2023-03-31": "the two sources do not report the "
                                                            "same count"})
        nse.corporate_actions = lambda *a, **k: actions_of("RELIANCE")
        year = {"2023-03-31": {"currency": "INR", "values": {}}}
        row = results.share_count_rows("TESTCO", year)[0]
        self.assertIsNone(row["shares"])
        self.assertIn("share basis changed", row["refused"][0])
        self.assertIn("2024-10-28", row["refused"][0])

    def test_an_undated_bonus_is_never_explained_away(self):
        """Reliance's real record with its bonus's ex-date made unreadable. Skipping it would
        print "No bonus or split since this year end explains it", which the record contradicts."""
        shares.confirmed = lambda s, w: ({}, {"2023-03-31": "the two sources do not report the "
                                                            "same count"})
        rows = [dict(r) for r in actions_of("RELIANCE")]
        next(r for r in rows if "bonus" in (r.get("subject") or "").lower())["exDate"] = "-"
        nse.corporate_actions = lambda *a, **k: rows
        year = {"2023-03-31": {"currency": "INR", "values": {}}}
        why = results.share_count_rows("TESTCO", year)[0]["refused"][0]
        self.assertNotIn("No bonus or split", why)
        self.assertIn("may have restated one of them", why)

    def test_a_disagreement_with_no_corporate_action_says_so(self):
        shares.confirmed = lambda s, w: ({}, {"2026-03-31": "the two sources do not report the "
                                                            "same count"})
        row = results.share_count_rows("TESTCO", self.YEAR)[0]
        self.assertIn("counting different things", row["refused"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
