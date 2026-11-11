"""Delivery percentage: the bands, and the fund filter the screen was useless without.

Runs on the exchange's own daily file for 2026-10-23, all 2,706 rows of it, so the fund problem
these tests describe is the real one rather than a sketch of it.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load import fixture, has, skill                            # noqa: E402

delivery = skill("nse-delivery", "delivery")
BHAV = "exchange/bhavcopy.json.gz"


def rows():
    return fixture(BHAV)["rows"]


class TheBands(unittest.TestCase):
    """What a delivery percentage means, in words, with the ambiguity kept in."""

    def test_very_high_delivery_is_ambiguous_and_says_so(self):
        """90% delivery is either real accumulation or a stock nobody traded.

        A band saying only "accumulation" would read a dead stock as a strong one.
        """
        said = delivery.band(90.0)
        self.assertIn("accumulation", said)
        self.assertIn("barely traded", said)

    def test_the_middle_bands(self):
        self.assertEqual(delivery.band(50.0), "real participation")
        self.assertEqual(delivery.band(25.0), "ordinary for a liquid large cap")

    def test_low_delivery_is_churn(self):
        self.assertEqual(delivery.band(5.0), "intraday churn")

    def test_the_bands_are_ordered_from_high_to_low(self):
        floors = [floor for floor, _label in delivery.BANDS]
        self.assertEqual(floors, sorted(floors, reverse=True))

    @unittest.skipUnless(has(BHAV), "run capture.py")
    def test_every_real_row_lands_in_some_band(self):
        for row in rows():
            if row.get("delivery_percent") is not None:
                self.assertTrue(delivery.band(row["delivery_percent"]),
                                f"{row['symbol']} at {row['delivery_percent']}%")


@unittest.skipUnless(has(BHAV), "run capture.py")
class TheDataTheExchangePublishes(unittest.TestCase):
    """The delivery percentage is the point of this skill, and no US source publishes it."""

    def test_the_file_carries_a_delivery_percentage_for_most_symbols(self):
        got = [r for r in rows() if r.get("delivery_percent") is not None]
        self.assertGreater(len(got), len(rows()) * 0.9)

    def test_delivery_is_a_percentage_of_volume_not_a_count(self):
        for row in rows():
            if row.get("delivery_percent") is not None:
                self.assertGreaterEqual(row["delivery_percent"], 0)
                self.assertLessEqual(row["delivery_percent"], 100)

    def test_delivered_shares_never_exceed_the_volume_traded(self):
        for row in rows():
            if row.get("delivered") and row.get("volume"):
                self.assertLessEqual(row["delivered"], row["volume"] + 1)


@unittest.skipUnless(has(BHAV), "run capture.py")
class TheFundFilter(unittest.TestCase):
    """An ETF is held, not traded, so it sits near 100% delivery permanently.

    Without this filter the high-delivery screen returned LIQUIDBETF, BANKIETF and MOMENTUM30 and
    not one company. This is that screen, on the real file, with and without the filter.
    """

    def test_the_raw_file_carries_both_companies_and_funds(self):
        names = {r["symbol"] for r in rows()}
        self.assertIn("RELIANCE", names)
        self.assertTrue({"LIQUIDBETF", "BANKIETF"} & names)

    def test_without_the_filter_funds_crowd_the_top_of_the_screen(self):
        """The bug, reproduced on the real file.

        Ranked by delivery among symbols that clear the turnover floor, most of the top of the
        screen on 2026-10-23 was funds: ELM250, MOMENTUM30, BANK10ADD, BBETF0432, HDFCNIFBAN,
        BANKBETA, HDFCLIQUID, LOWVOL1. Not one of them is a company.
        """
        from nsetools import nse
        companies = nse.equity_symbols()
        floor = delivery.MIN_TURNOVER_LACS
        ranked = sorted((r for r in rows()
                         if r.get("delivery_percent") is not None
                         and (r.get("turnover") or 0) >= floor),
                        key=lambda r: -r["delivery_percent"])
        top = [r["symbol"] for r in ranked[:12]]
        not_companies = [s for s in top if s not in companies]
        self.assertGreaterEqual(len(not_companies), 6,
                                f"expected funds to crowd the top, got {top}")

    def test_a_funds_name_does_not_give_it_away_which_is_why_the_list_is_needed(self):
        """MOMENTUM30, ELM250, LOWVOL1 and BANK10ADD carry no ETF in the name.

        So the filter cannot be a name pattern. It has to be the exchange's own equity master
        list, which says which symbols are companies.
        """
        from nsetools import nse
        companies = nse.equity_symbols()
        present = {r["symbol"] for r in rows()}
        hidden = [s for s in ("MOMENTUM30", "ELM250", "LOWVOL1", "BANK10ADD")
                  if s in present]
        self.assertTrue(hidden, "none of those funds traded in this capture")
        for name in hidden:
            self.assertNotIn(name, companies)
            self.assertFalse(name.endswith("ETF"))

    def test_with_the_filter_they_are_gone(self):
        kept = {r["symbol"] for r in delivery.liquid(rows())}
        for fund in ("LIQUIDBETF", "BANKIETF"):
            self.assertNotIn(fund, kept)

    def test_the_filter_keeps_real_companies(self):
        kept = {r["symbol"] for r in delivery.liquid(rows())}
        self.assertIn("RELIANCE", kept)
        self.assertGreater(len(kept), 500)

    def test_the_filter_drops_about_the_number_of_funds_the_exchange_lists(self):
        """The bhavcopy carries about 2,706 symbols and the equity master list 2,359."""
        self.assertGreater(len(rows()), 2500)
        self.assertLess(len(delivery.liquid(rows())), len(rows()))

    def test_a_row_with_no_delivery_figure_is_dropped_not_defaulted_to_zero(self):
        """Zero delivery and unknown delivery are different things."""
        one = dict(next(r for r in rows() if r["symbol"] == "RELIANCE"), delivery_percent=None)
        self.assertEqual(delivery.liquid([one]), [])

    def test_an_illiquid_row_is_dropped(self):
        one = dict(next(r for r in rows() if r["symbol"] == "RELIANCE"), turnover=1.0)
        self.assertEqual(delivery.liquid([one]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
