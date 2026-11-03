"""Accounts with no key, and the four field names that mean something other than they say."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nsetools import yahoo                                        # noqa: E402

CRORE = 1e7


def _load(folder: str, module: str):
    spec = importlib.util.spec_from_file_location(module, ROOT / folder / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


results = _load("nse-results", "results")


def year(**values):
    return {"currency": values.pop("currency", "INR"),
            "values": {k: v * CRORE for k, v in values.items()}}


class NoCredentials(unittest.TestCase):
    """The whole pack asks for nothing. This file is where that is pinned down."""

    def test_the_accounts_module_takes_no_key_of_any_kind(self):
        import inspect
        args = inspect.signature(yahoo.accounts).parameters
        for forbidden in ("token", "key", "api_key", "access", "secret"):
            self.assertNotIn(forbidden, args)

    def test_nothing_in_the_module_reads_an_environment_credential(self):
        source = (ROOT / "nsetools" / "yahoo.py").read_text(encoding="utf-8")
        self.assertNotIn("environ", source)
        self.assertNotIn("getenv", source)


class FieldNamesThatLie(unittest.TestCase):
    """Four of them, each measured against audited accounts rather than read off the docs."""

    def test_net_income_is_the_owners_share_not_the_group(self):
        """Reliance 2026: 80,775 crore to owners against a group profit of 95,754.

        Calling it "net income" and comparing it with another source's group profit manufactures a
        15% disagreement that was never a disagreement.
        """
        self.assertEqual(yahoo.FIELDS["annualNetIncome"], "net_profit_owners")
        self.assertEqual(yahoo.FIELDS["annualNetIncomeIncludingNoncontrollingInterests"],
                         "net_profit")

    def test_the_cash_position_comes_from_the_right_field(self):
        """annualCashAndCashEquivalents sounds right and is only part of the position.

        Against Reliance's audited balance sheets 2023-26 the closing cash from the cash flow
        statement is exact all four years; the field that sounds right is out by 3.8% to 50.3%.
        """
        self.assertEqual(yahoo.FIELDS["annualEndCashPosition"], "cash")
        self.assertNotIn("annualCashAndCashEquivalents", yahoo.FIELDS)

    def test_the_share_count_is_warned_about(self):
        """The worst of the four: it is today's count against every historical year.

        Reliance's 1:1 bonus went ex 2024-10-28, and this field reports 1,353 crore shares against
        FY2023 and FY2024, which both closed before it, where the real count was 676.6 crore.
        """
        self.assertIn("shares", yahoo.WARNED)
        self.assertIn("today", yahoo.WARNED["shares"])

    def test_eps_is_warned_about_as_restated(self):
        for key in ("eps_basic", "eps_diluted"):
            self.assertIn(key, yahoo.WARNED)
            self.assertIn("restated", yahoo.WARNED[key])

    def test_every_warned_concept_is_actually_mapped(self):
        mapped = set(yahoo.FIELDS.values())
        for concept in yahoo.WARNED:
            self.assertIn(concept, mapped)


class Contradictions(unittest.TestCase):
    """Four years from one source means no cross-source check. Arithmetic is what is left."""

    def test_equity_above_assets_is_reported(self):
        trouble = results.contradictions({"2026-03-31": year(assets=1000, equity=1200)})
        self.assertTrue(any("cannot be" in t for t in trouble))

    def test_reliances_real_figures_raise_nothing(self):
        trouble = results.contradictions({"2026-03-31": year(
            revenue=1057219, profit_before_tax=123162, assets=2178140, equity=904030)})
        self.assertEqual(trouble, [])

    def test_a_foreign_currency_is_refused_not_converted(self):
        """One source reports Infosys in dollars; against a rupee price that is 80x wrong."""
        trouble = results.contradictions({"2026-03-31": year(revenue=100, currency="USD")})
        self.assertTrue(any("not rupees" in t for t in trouble))
        self.assertTrue(any("not converted" in t for t in trouble))

    def test_negative_assets_are_reported(self):
        trouble = results.contradictions({"2026-03-31": year(assets=-50)})
        self.assertTrue(any("negative" in t for t in trouble))

    def test_a_year_missing_figures_is_not_judged(self):
        self.assertEqual(results.contradictions({"2026-03-31": year(revenue=100)}), [])


class Ratios(unittest.TestCase):
    def test_return_on_equity_uses_the_owners_figures_on_both_sides(self):
        """Market value is a claim on the owners' share, so both halves must be theirs.

        Mixing the group's profit with the owners' equity is the usual way these come out wrong.
        """
        rows = results.ratios({"2026-03-31": year(
            net_profit_owners=80775, net_profit=95754, equity=904030, assets=2178140)})
        self.assertAlmostEqual(rows[0]["roe_percent"], round(80775 / 904030 * 100, 1), places=1)

    def test_return_on_assets_uses_the_groups_figures_on_both_sides(self):
        rows = results.ratios({"2026-03-31": year(
            net_profit_owners=80775, net_profit=95754, equity=904030, assets=2178140)})
        self.assertAlmostEqual(rows[0]["roa_percent"], round(95754 / 2178140 * 100, 1), places=1)

    def test_a_missing_figure_gives_no_ratio_rather_than_a_zero(self):
        rows = results.ratios({"2026-03-31": year(revenue=1000)})
        self.assertIsNone(rows[0]["roe_percent"])
        self.assertIsNone(rows[0]["debt_to_equity"])

    def test_zero_equity_does_not_divide(self):
        rows = results.ratios({"2026-03-31": year(net_profit_owners=100, equity=0)})
        self.assertIsNone(rows[0]["roe_percent"])


class FourYears(unittest.TestCase):
    def test_the_cap_is_documented_as_a_cap(self):
        """period1 set to 2010, 2000 or 1970 all return the same four years."""
        source = (ROOT / "nsetools" / "yahoo.py").read_text(encoding="utf-8")
        self.assertIn("hard cap", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
