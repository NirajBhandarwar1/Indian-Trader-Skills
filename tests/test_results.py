"""Accounts with no key, and the four field names that mean something other than they say.

EVERY FIGURE HERE IS A REAL RESPONSE. The accounts are the saved keyless responses for RELIANCE,
TCS, SBIN and ITC; the parser under test is the real one, fed those bytes. The figures asserted
were checked against Reliance's audited annual report when the mapping was first worked out.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import has, raw, skill                                # noqa: E402
from nsetools import yahoo                                       # noqa: E402

results = skill("nse-results", "results")

CRORE = 1e7
ACCOUNTS = {s: f"accounts/{s}.json" for s in ("RELIANCE", "TCS", "SBIN", "ITC")}


class Feed:
    """Hands the real saved response to the real parser, so the parser is what is tested."""

    def __init__(self, symbol):
        self.body = raw(ACCOUNTS[symbol])
        self.real = yahoo._fetch

    def __enter__(self):
        yahoo._fetch = lambda url, timeout=30: self.body
        return self

    def __exit__(self, *_):
        yahoo._fetch = self.real


def accounts_of(symbol: str) -> dict:
    with Feed(symbol):
        return yahoo.accounts(symbol)


def crore(value):
    return None if value is None else round(value / CRORE)


@unittest.skipUnless(all(has(p) for p in ACCOUNTS.values()), "run capture.py")
class TheRealResponseParses(unittest.TestCase):
    def test_four_years_come_back_and_that_is_the_cap(self):
        """period1 set to 0 still returns four. It is a hard cap on this endpoint."""
        self.assertEqual(len(accounts_of("RELIANCE")), 4)

    def test_the_latest_year_is_the_last_completed_financial_year(self):
        """Indian companies close in March, so FY2026 ended 2026-03-31."""
        self.assertEqual(max(accounts_of("RELIANCE")), "2026-03-31")

    def test_every_year_is_in_rupees(self):
        for year in accounts_of("RELIANCE").values():
            self.assertTrue(yahoo.in_rupees(year))

    def test_all_four_companies_parse(self):
        for symbol in ACCOUNTS:
            self.assertGreaterEqual(len(accounts_of(symbol)), 4, symbol)

    def test_reliances_real_figures_for_the_latest_year(self):
        """Checked against the audited annual report when this mapping was worked out."""
        values = accounts_of("RELIANCE")["2026-03-31"]["values"]
        self.assertEqual(crore(values["revenue"]), 1057219)
        self.assertEqual(crore(values["profit_before_tax"]), 123162)
        self.assertEqual(crore(values["assets"]), 2178140)
        self.assertEqual(crore(values["equity"]), 904030)
        self.assertEqual(crore(values["cash"]), 145977)
        self.assertEqual(crore(values["cash_from_operations"]), 192113)


@unittest.skipUnless(has(ACCOUNTS["RELIANCE"]), "run capture.py")
class FieldNamesThatLie(unittest.TestCase):
    """Two of the four are fixed by reading the right field and naming it honestly."""

    def setUp(self):
        self.values = accounts_of("RELIANCE")["2026-03-31"]["values"]

    def test_net_income_is_the_owners_share_not_the_group(self):
        """80,775 crore to owners against a group profit of 95,754, in the real response.

        Calling the first "net income" and comparing it with another source's group profit
        manufactures a 15% disagreement that was never a disagreement.
        """
        self.assertEqual(crore(self.values["net_profit_owners"]), 80775)
        self.assertEqual(crore(self.values["net_profit"]), 95754)
        self.assertEqual(yahoo.FIELDS["annualNetIncome"], "net_profit_owners")
        self.assertEqual(yahoo.FIELDS["annualNetIncomeIncludingNoncontrollingInterests"],
                         "net_profit")
        gap = abs(self.values["net_profit"] - self.values["net_profit_owners"])
        self.assertGreater(gap / self.values["net_profit"], 0.1)

    def test_the_cash_position_comes_from_the_cash_flow_statement(self):
        """The field that sounds right is in the same response and is not the cash position.

        Against the audited balance sheets 2023-26 the closing cash from the cash flow statement
        is exact all four years; annualCashAndCashEquivalents is out by 3.8% to 50.3%. So it is
        not mapped at all.
        """
        self.assertEqual(yahoo.FIELDS["annualEndCashPosition"], "cash")
        self.assertNotIn("annualCashAndCashEquivalents", yahoo.FIELDS)
        self.assertEqual(crore(self.values["cash"]), 145977)

    def test_earnings_per_share_is_not_read_at_all(self):
        """It is restated to the current share basis and has no second source here.

        The only check available is arithmetic against the share count, and that cannot be exact:
        Reliance FY2023 derives 49.2910 against a published 49.295, which is the rounding in an
        as-filed 98.59 carried through a halving. So no tolerance decides whether to show it.
        """
        for field in ("annualBasicEPS", "annualDilutedEPS"):
            self.assertNotIn(field, yahoo.FIELDS)
        self.assertNotIn("eps_basic", set(yahoo.FIELDS.values()))
        for year in accounts_of("RELIANCE").values():
            self.assertNotIn("eps_basic", year["values"])

    def test_the_share_count_is_proved_before_it_becomes_data(self):
        self.assertIn("shares", yahoo.VERIFIED_AGAINST)
        self.assertIn("match exactly", yahoo.VERIFIED_AGAINST["shares"])
        self.assertNotIn("WARNED", dir(yahoo))


@unittest.skipUnless(has(ACCOUNTS["RELIANCE"]), "run capture.py")
class CleanDataNotJustACleanScreen(unittest.TestCase):
    """An unproved figure must not enter the data, not merely be left off the screen.

    Whatever accounts() returns is what --save writes to nse/results.json, and nse-scan reads that
    file. A figure hidden only when printing still travels into another skill's input.
    """

    def test_the_accounts_request_never_even_asks_for_a_share_count(self):
        """This is the mechanism, not a filter afterwards.

        The field list sent to the endpoint is FIELDS, which does not contain the share count, so
        the response cannot carry one and nothing downstream has to remember to drop it. The
        saved response is the proof: it has no share count in it.
        """
        body = raw(ACCOUNTS["RELIANCE"]).decode("utf-8")
        self.assertNotIn("OrdinarySharesNumber", body)
        for year in accounts_of("RELIANCE").values():
            self.assertNotIn("shares", year["values"])

    def test_the_share_count_is_kept_out_of_the_accounts_map(self):
        self.assertNotIn(yahoo.UNCONFIRMED_SHARES, yahoo.FIELDS)
        self.assertNotIn("shares", set(yahoo.FIELDS.values()))

    def test_the_fetcher_reads_nothing_beyond_the_fields_asked_for(self):
        import inspect
        source = inspect.getsource(yahoo._series)
        self.assertIn("wanted.get(key)", source)
        self.assertNotIn("FIELDS", source)

    @unittest.skipUnless(has("accounts/RELIANCE-shares-unconfirmed.json"), "run capture.py")
    def test_the_unconfirmed_count_is_fetched_separately_and_named_as_unconfirmed(self):
        body = raw("accounts/RELIANCE-shares-unconfirmed.json")
        real = yahoo._fetch
        yahoo._fetch = lambda url, timeout=30: body
        try:
            got = yahoo.unconfirmed_share_counts("RELIANCE")
        finally:
            yahoo._fetch = real
        self.assertTrue(got)
        self.assertIn("UNCONFIRMED", yahoo.unconfirmed_share_counts.__doc__)
        self.assertEqual(round(got[max(got)]), 13532472634)
        # It moves year to year rather than being frozen at today's count, which is what made it
        # look usable. FY2025 reads higher than FY2026 because shares were bought back.
        self.assertNotEqual(len(set(got.values())), 1)


@unittest.skipUnless(all(has(p) for p in ACCOUNTS.values()), "run capture.py")
class Contradictions(unittest.TestCase):
    """Four years from one source means no cross-source check. Arithmetic is what is left."""

    def test_no_real_company_in_the_capture_contradicts_itself(self):
        for symbol in ACCOUNTS:
            for when, year in accounts_of(symbol).items():
                self.assertEqual(results.contradictions({when: year}), [],
                                 f"{symbol} {when}")

    def test_equity_above_assets_is_reported(self):
        real = accounts_of("RELIANCE")["2026-03-31"]
        broken = {"2026-03-31": {"currency": "INR", "values": dict(
            real["values"], equity=real["values"]["assets"] * 1.5)}}
        self.assertTrue(any("cannot be" in t for t in results.contradictions(broken)))

    def test_a_foreign_currency_is_refused_not_converted(self):
        """One source reports Infosys in dollars; against a rupee price that is 80x wrong."""
        real = accounts_of("RELIANCE")["2026-03-31"]
        in_dollars = {"2026-03-31": {"currency": "USD", "values": real["values"]}}
        trouble = results.contradictions(in_dollars)
        self.assertTrue(any("not rupees" in t for t in trouble))
        self.assertTrue(any("not converted" in t for t in trouble))

    def test_negative_assets_are_reported(self):
        real = accounts_of("RELIANCE")["2026-03-31"]
        broken = {"2026-03-31": {"currency": "INR",
                                 "values": dict(real["values"], assets=-50 * CRORE)}}
        self.assertTrue(any("negative" in t for t in results.contradictions(broken)))

    def test_a_year_missing_figures_is_not_judged(self):
        self.assertEqual(results.contradictions(
            {"2026-03-31": {"currency": "INR", "values": {"revenue": 100.0}}}), [])


@unittest.skipUnless(all(has(p) for p in ACCOUNTS.values()), "run capture.py")
class Ratios(unittest.TestCase):
    def test_return_on_equity_uses_the_owners_figures_on_both_sides(self):
        """Market value is a claim on the owners' share, so both halves must be theirs.

        Mixing the group's profit with the owners' equity is the usual way these come out wrong,
        and the two differ by 15% in this very response.
        """
        year = accounts_of("RELIANCE")["2026-03-31"]
        rows = results.ratios({"2026-03-31": year})
        expected = round(year["values"]["net_profit_owners"] / year["values"]["equity"] * 100, 1)
        self.assertEqual(rows[0]["roe_percent"], expected)

    def test_return_on_assets_uses_the_groups_figures_on_both_sides(self):
        year = accounts_of("RELIANCE")["2026-03-31"]
        rows = results.ratios({"2026-03-31": year})
        expected = round(year["values"]["net_profit"] / year["values"]["assets"] * 100, 1)
        self.assertEqual(rows[0]["roa_percent"], expected)

    def test_the_real_ratios_land_where_the_companies_really_are(self):
        """TCS is a high-return services business and Reliance is capital heavy.

        If these two came out the same way round, something is mixed up.
        """
        tcs = results.ratios(accounts_of("TCS"))[0]["roe_percent"]
        reliance = results.ratios(accounts_of("RELIANCE"))[0]["roe_percent"]
        self.assertGreater(tcs, 30)
        self.assertLess(reliance, 20)

    def test_a_missing_figure_gives_no_ratio_rather_than_a_zero(self):
        rows = results.ratios({"2026-03-31": {"currency": "INR",
                                              "values": {"revenue": 1000.0}}})
        self.assertIsNone(rows[0]["roe_percent"])
        self.assertIsNone(rows[0]["debt_to_equity"])

    def test_zero_equity_does_not_divide(self):
        rows = results.ratios({"2026-03-31": {"currency": "INR", "values": {
            "net_profit_owners": 100.0, "equity": 0.0}}})
        self.assertIsNone(rows[0]["roe_percent"])


class NoCredentials(unittest.TestCase):
    """The whole pack asks for nothing. This file is where that is pinned down."""

    def test_the_accounts_module_takes_no_key_of_any_kind(self):
        import inspect
        args = inspect.signature(yahoo.accounts).parameters
        for forbidden in ("token", "key", "api_key", "access", "secret"):
            self.assertNotIn(forbidden, args)

    def test_nothing_in_the_module_reads_an_environment_credential(self):
        source = (Path(__file__).resolve().parent.parent
                  / "nsetools" / "yahoo.py").read_text(encoding="utf-8")
        self.assertNotIn("environ", source)
        self.assertNotIn("getenv", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
