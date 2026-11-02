"""Fundamentals: the unit guard, the refused lines, and a year that contradicts itself."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nsetools import upstox                                        # noqa: E402

CRORE = 1e7


def _load(folder: str, module: str):
    spec = importlib.util.spec_from_file_location(module, ROOT / folder / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


results = _load("nse-results", "results")


def statement(lines, units="crore", period="Mar 2026"):
    return {"status": "success", "data": {
        "type": "consolidated", "units_in": units,
        "full_statement": [{"particular": name, "history": [{"period": period, "value": v}]}
                           for name, v in lines.items()]}}


class Units(unittest.TestCase):
    def test_crore_becomes_rupees(self):
        """A crore kept as a crore is a figure ten million times too small."""
        got = upstox.figures(statement({"Total Assets": 2178140}), upstox.BALANCE_SHEET, "X")
        self.assertEqual(got["2026-03-31"]["assets"], 2178140 * CRORE)

    def test_an_unknown_unit_is_refused_not_converted(self):
        """Guessing at an unknown unit is how a plausible wrong number gets in."""
        with self.assertRaises(upstox.Refused):
            upstox.figures(statement({"Total Assets": 2178140}, units="million"),
                           upstox.BALANCE_SHEET, "X")

    def test_a_missing_unit_is_assumed_to_be_crore(self):
        got = upstox.figures(statement({"Total Assets": 100}, units=""), upstox.BALANCE_SHEET, "X")
        self.assertEqual(got["2026-03-31"]["assets"], 100 * CRORE)


class RefusedLines(unittest.TestCase):
    """Five lines are deliberately not read. Each was checked against audited accounts."""

    def test_none_of_the_refused_lines_is_also_mapped(self):
        mapped = (set(upstox.BALANCE_SHEET) | set(upstox.INCOME_STATEMENT)
                  | set(upstox.CASH_FLOW))
        for line in upstox.REFUSED_LINES:
            self.assertNotIn(line, mapped, f"{line} is refused and mapped at the same time")

    def test_equity_capital_is_ignored_even_when_present(self):
        # 10,85,866 against an audited 9,04,030: it includes minority interest
        got = upstox.figures(statement({"Equity Capital": 1085866}), upstox.BALANCE_SHEET, "X")
        self.assertEqual(got, {})

    def test_profit_after_tax_is_ignored(self):
        # struck before the share of associates: 95,610 against 95,754
        got = upstox.figures(statement({"Profit After Tax": 95610}),
                            upstox.INCOME_STATEMENT, "X")
        self.assertEqual(got, {})

    def test_eps_is_ignored_because_the_series_mixes_vintages(self):
        """Reliance 2023-26 reads 98.59, 51.45, 51.47, 59.69.

        The 2024 figure is the comparative restated after that October's bonus; 2023 is still as
        filed. Every number is correct and the series is broken, which is the dangerous shape.
        """
        got = upstox.figures(statement({"EPS - Basic": 51.45, "EPS - Diluted": 51.45}),
                            upstox.INCOME_STATEMENT, "X")
        self.assertEqual(got, {})

    def test_the_lines_that_are_read_still_come_through(self):
        got = upstox.figures(
            statement({"Revenue": 1057219, "Other Income": 28962, "Profit Before Tax": 123162,
                       "Total Revenue": 1086181}), upstox.INCOME_STATEMENT, "X")
        v = got["2026-03-31"]
        self.assertEqual(v["revenue"], 1057219 * CRORE)
        self.assertEqual(v["other_income"], 28962 * CRORE)
        self.assertEqual(v["profit_before_tax"], 123162 * CRORE)
        self.assertNotIn("total_revenue", v)


class Periods(unittest.TestCase):
    def test_a_period_becomes_the_day_it_ends_on(self):
        self.assertEqual(upstox.period_end("Mar 2026"), "2026-03-31")
        self.assertEqual(upstox.period_end("Jun 2026"), "2026-06-30")
        self.assertEqual(upstox.period_end("Dec 2025"), "2025-12-31")

    def test_an_unreadable_period_becomes_nothing_not_a_guess(self):
        self.assertIsNone(upstox.period_end("sometime"))
        self.assertIsNone(upstox.period_end(""))
        self.assertIsNone(upstox.period_end("FY26"))


class YearsThatContradictThemselves(unittest.TestCase):
    """The only check available on a figure one source reports alone, which is most of them."""

    def test_profit_above_total_income_is_reported(self):
        trouble = results.adds_up({"2026-03-31": {
            "revenue": 1000 * CRORE, "other_income": 50 * CRORE,
            "profit_before_tax": 2000 * CRORE}})
        self.assertTrue(trouble)
        self.assertIn("exceeds revenue plus", trouble[0])

    def test_an_ordinary_year_is_silent(self):
        trouble = results.adds_up({"2026-03-31": {
            "revenue": 1057219 * CRORE, "other_income": 28962 * CRORE,
            "profit_before_tax": 123162 * CRORE, "assets": 2178140 * CRORE}})
        self.assertEqual(trouble, [])

    def test_negative_assets_are_reported(self):
        trouble = results.adds_up({"2026-03-31": {"assets": -100 * CRORE}})
        self.assertTrue(any("negative" in t for t in trouble))

    def test_a_year_missing_figures_is_not_judged(self):
        """A gap is not a contradiction."""
        self.assertEqual(results.adds_up({"2026-03-31": {"revenue": 100 * CRORE}}), [])


class Tokens(unittest.TestCase):
    def test_no_token_anywhere_raises_something_readable(self):
        import os
        kept = os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                here = Path.cwd()
                os.chdir(tmp)
                try:
                    with self.assertRaises(upstox.NoToken) as caught:
                        upstox.token()
                    self.assertIn("upstox.com/developer", str(caught.exception))
                finally:
                    os.chdir(here)
        finally:
            if kept is not None:
                os.environ["UPSTOX_ACCESS_TOKEN"] = kept

    def test_an_explicit_token_wins(self):
        self.assertEqual(upstox.token("abc123"), "abc123")

    def test_a_config_file_is_read(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text(json.dumps({"upstox_access_token": "from-file"}))
            here = Path.cwd()
            os.chdir(tmp)
            try:
                self.assertEqual(upstox.token(), "from-file")
            finally:
                os.chdir(here)


class RateLimit(unittest.TestCase):
    def test_the_budget_is_under_half_the_published_limit(self):
        """Published is 2,000 per 30 minutes, and only that window binds.

        A run paced to 210 a minute obeyed the per-second and per-minute limits and was still cut
        off after 1,950 requests.
        """
        self.assertLess(upstox.WINDOW_BUDGET, 1000)
        self.assertEqual(upstox.WINDOW_SECONDS, 30 * 60)
        self.assertLessEqual(upstox.REQUESTS_PER_SECOND, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
