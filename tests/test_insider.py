"""Telling a desk turning its book from somebody taking a view. Offline, on real shapes."""
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


insider = _load("nse-insider", "insider")


def deal(date, symbol, client, side, shares, price):
    return {"date": date, "symbol": symbol, "client": client, "side": side,
            "shares": float(shares), "price": float(price),
            "value_crore": round(shares * price / 1e7, 2), "company": ""}


class RoundTrips(unittest.TestCase):
    """The trap that makes bulk deal data misleading: 62 of 70 disclosed deals were two-sided."""

    def test_the_same_client_both_sides_flat_is_a_round_trip(self):
        # MICROCURVES, CMLL, 2026-10-13: sold 2,099,164 at 633.54 and bought the same at 633.26
        rows = [deal("2026-10-13", "CMLL", "MICROCURVES", "SELL", 2099164, 633.54),
                deal("2026-10-13", "CMLL", "MICROCURVES", "BUY", 2099164, 633.26)]
        insider.mark_round_trips(rows)
        self.assertTrue(all(r["round_trip"] for r in rows))
        self.assertTrue(all("flat by the close" in r["note"] for r in rows))

    def test_uneven_legs_are_still_a_round_trip(self):
        """IRAGE on CMLL: 33.70 crore bought against 28.73 crore sold, 15% apart.

        A 10% flatness band was tried first and let this through as a real position. It is a desk
        working an order, so both-sides-at-all is the test, not how square the legs are.
        """
        rows = [deal("2026-10-13", "CMLL", "IRAGE BROKING", "BUY", 53200, 633.46),
                deal("2026-10-13", "CMLL", "IRAGE BROKING", "SELL", 45350, 633.46)]
        insider.mark_round_trips(rows)
        self.assertTrue(all(r["round_trip"] for r in rows))
        self.assertTrue(any("net" in r["note"] for r in rows))

    def test_two_different_clients_are_a_real_transfer(self):
        # AQYLON, 2026-10-13: one party sold, a different party bought. That is a position changing
        # hands, which is the thing worth looking at.
        rows = [deal("2026-10-13", "AQYLON", "LEADING LEASING", "SELL", 12170520, 14.99),
                deal("2026-10-13", "AQYLON", "KAMDAR HUF", "BUY", 11000000, 15.00)]
        insider.mark_round_trips(rows)
        self.assertFalse(any(r["round_trip"] for r in rows))
        self.assertTrue(all(r["note"] == "" for r in rows))

    def test_one_side_only_is_never_a_round_trip(self):
        rows = [deal("2026-10-22", "ADANIENSOL", "PI OPPORTUNITIES AIF V LLP", "BUY",
                     4953560, 1334.0)]
        insider.mark_round_trips(rows)
        self.assertFalse(rows[0]["round_trip"])

    def test_the_same_client_on_different_days_is_not_a_round_trip(self):
        rows = [deal("2026-10-13", "CMLL", "MICROCURVES", "BUY", 100000, 600.0),
                deal("2026-10-14", "CMLL", "MICROCURVES", "SELL", 100000, 610.0)]
        insider.mark_round_trips(rows)
        self.assertFalse(any(r["round_trip"] for r in rows))

    def test_the_same_client_in_different_stocks_is_not_a_round_trip(self):
        rows = [deal("2026-10-13", "CMLL", "MICROCURVES", "BUY", 100000, 600.0),
                deal("2026-10-13", "IXIGO", "MICROCURVES", "SELL", 100000, 158.0)]
        insider.mark_round_trips(rows)
        self.assertFalse(any(r["round_trip"] for r in rows))

    def test_the_deal_is_labelled_not_deleted(self):
        """A round trip did happen and the price is real, so nothing is dropped silently."""
        rows = [deal("2026-10-13", "CMLL", "MICROCURVES", "SELL", 2099164, 633.54),
                deal("2026-10-13", "CMLL", "MICROCURVES", "BUY", 2099164, 633.26)]
        insider.mark_round_trips(rows)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["value_crore"], 132.99)


class Parsing(unittest.TestCase):
    def test_rupee_values_survive_commas(self):
        self.assertEqual(insider.rupees("1,765,231"), 1765231.0)
        self.assertEqual(insider.rupees("9.22"), 9.22)
        self.assertIsNone(insider.rupees("-"))
        self.assertIsNone(insider.rupees(None))

    def test_the_exchange_date_formats_all_normalise(self):
        self.assertEqual(insider.day_of("19-OCT-2026"), "2026-10-19")
        self.assertEqual(insider.day_of("06-Apr-2026 16:07"), "2026-04-06")
        self.assertEqual(insider.day_of("27-Mar-2026"), "2026-03-27")

    def test_an_unparseable_date_is_passed_through_not_invented(self):
        self.assertEqual(insider.day_of("not a date"), "not a date")

    def test_market_purchase_counts_as_telling_and_esop_does_not(self):
        """An employee exercising options is not somebody choosing to buy."""
        self.assertIn("market purchase", insider.TELLING)
        self.assertNotIn("esop", insider.TELLING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
