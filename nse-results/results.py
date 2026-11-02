#!/usr/bin/env python3
"""Company fundamentals, with the lines that lie left out and said out loud.

Four years of accounts per company, and the published ratios against the sector. The exchange
stopped publishing structured financial statements in December 2024, so this needs an Upstox token
-- the only skill in the pack that does. Without one it says so and exits cleanly.

Standard library only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nsetools import nse, show, upstox                           # noqa: E402
from nsetools.http import NotPublished                            # noqa: E402

CRORE = 1e7


def isin_for(symbol: str) -> str:
    """The exchange's own master list is the mapping, and it needs no token."""
    for row in nse.equities():
        if row.get("symbol", "").upper() == symbol.upper():
            got = row.get("isin") or ""
            if got:
                return got
    raise NotPublished(f"{symbol} is not in the exchange's equity master list")


def accounts(symbol: str, access: str, basis: str = "consolidated") -> dict:
    """Everything mapped, by period. Nothing is derived and nothing is filled in."""
    isin = isin_for(symbol)
    by_period: dict[str, dict[str, float]] = {}
    for slug, mapping in upstox.STATEMENTS:
        body = upstox.fetch(f"{upstox.BASE}/{isin}/{slug}", access,
                            {"fs": "true", "type": basis})
        for when, values in upstox.figures(body, mapping, symbol).items():
            by_period.setdefault(when, {}).update(values)
    return by_period


def published_ratios(symbol: str, access: str, basis: str = "consolidated") -> dict:
    isin = isin_for(symbol)
    return upstox.ratios(upstox.fetch(f"{upstox.BASE}/{isin}/key-ratios", access, {"type": basis}))


def adds_up(by_period: dict) -> list[str]:
    """Arithmetic the source published both sides of, so it needs no second opinion.

    A statement that does not add up is wrong however plausible each line looks. This is the only
    check available on a figure one source reports alone, which is most of them.
    """
    trouble = []
    for when in sorted(by_period):
        v = by_period[when]
        rev, other = v.get("revenue"), v.get("other_income")
        pbt = v.get("profit_before_tax")
        if rev is not None and other is not None and pbt is not None and rev:
            # Revenue plus other income is total income, and profit before tax cannot exceed it.
            if pbt > (rev + other) * 1.001:
                trouble.append(f"{when}: profit before tax {pbt/CRORE:,.0f} exceeds revenue plus "
                               f"other income {(rev+other)/CRORE:,.0f}, which cannot be")
        assets = v.get("assets")
        if assets is not None and assets < 0:
            trouble.append(f"{when}: total assets {assets/CRORE:,.0f} is negative")
        if rev is not None and rev < 0:
            trouble.append(f"{when}: revenue {rev/CRORE:,.0f} is negative")
    return trouble


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--basis", default="consolidated", choices=["consolidated", "standalone"])
    ap.add_argument("--ratios", action="store_true", help="the published ratios against the sector")
    ap.add_argument("--token", help="overrides config.json and the environment")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        access = upstox.token(args.token)
    except upstox.NoToken as err:
        print(err)
        return 4

    symbol = args.symbol.upper()
    try:
        if args.ratios:
            got = published_ratios(symbol, access, args.basis)
            rows = [{"ratio": k, "company": v.get("company"), "sector": v.get("sector")}
                    for k, v in got.items()]
            if not args.json:
                print(f"{symbol}, {args.basis}, as the source publishes them\n")
            show.emit(rows, args.json,
                      [("ratio", "ratio", 14), ("company", "company", 12), ("sector", "sector", 12)],
                      empty="No ratios published for this company.",
                      note="These are the source's own arithmetic, not this skill's. Where they "
                           "disagree with your own, one of the figures underneath is wrong.")
            return 0

        by_period = accounts(symbol, access, args.basis)
        if not by_period:
            print(f"{symbol}: nothing reported. Smaller listings often have no statements here.")
            return 0

        rows = []
        for when in sorted(by_period, reverse=True):
            v = by_period[when]
            rows.append({
                "year_end": when,
                "revenue": round(v["revenue"] / CRORE) if "revenue" in v else None,
                "other_income": round(v["other_income"] / CRORE) if "other_income" in v else None,
                "profit_before_tax": round(v["profit_before_tax"] / CRORE)
                if "profit_before_tax" in v else None,
                "assets": round(v["assets"] / CRORE) if "assets" in v else None,
                "cash": round(v["cash"] / CRORE) if "cash" in v else None,
                "cash_from_operations": round(v["cash_from_operations"] / CRORE)
                if "cash_from_operations" in v else None,
            })

        if not args.json:
            print(f"{symbol}, {args.basis} accounts as reported, in crore\n")
        show.emit(rows, args.json,
                  [("year_end", "year end", 11), ("revenue", "revenue", 11),
                   ("other_income", "other inc", 10), ("profit_before_tax", "pbt", 10),
                   ("assets", "assets", 12), ("cash", "cash", 10),
                   ("cash_from_operations", "cash from ops", 14)],
                  empty="Nothing reported.")

        trouble = adds_up(by_period)
        if trouble:
            print("\n   THESE FIGURES CONTRADICT EACH OTHER:")
            for line in trouble:
                print(f"      {line}")
            print("      Nothing should be computed from a year that does not add up.")
        if not args.json:
            print(f"\n   Five lines this source publishes are deliberately NOT shown: "
                  f"{', '.join(upstox.REFUSED_LINES)}.")
            print(f"   Each would be a plausible wrong number. Run --help on the module for why.")
        return 0

    except upstox.Refused as err:
        print(f"The source would not answer: {err}")
        return 2
    except NotPublished as err:
        print(f"{err}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
