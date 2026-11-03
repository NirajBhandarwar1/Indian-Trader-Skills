#!/usr/bin/env python3
"""Company accounts, with no key, no account and no signup.

Four years per company, plus the ratios they imply, plus the four field names that mean something
other than they say. Checks each year against itself, because for most of these figures there is no
second opinion to check them against.

Optionally cross-checks against a second source where a token happens to be configured, which is
the one genuinely useful thing a token buys here. Nothing requires it.

Standard library only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nsetools import show, yahoo                                 # noqa: E402

CRORE = 1e7


def crore(value):
    return round(value / CRORE) if value is not None else None


def table_rows(by_year: dict) -> list[dict]:
    out = []
    for when in sorted(by_year, reverse=True):
        v = by_year[when]["values"]
        out.append({
            "year_end": when,
            "revenue": crore(v.get("revenue")),
            "pbt": crore(v.get("profit_before_tax")),
            "profit": crore(v.get("net_profit_owners")),
            "assets": crore(v.get("assets")),
            "equity": crore(v.get("equity")),
            "debt": crore(v.get("debt")),
            "cash": crore(v.get("cash")),
            "cash_from_ops": crore(v.get("cash_from_operations")),
            "currency": by_year[when]["currency"],
        })
    return out


def ratios(by_year: dict) -> list[dict]:
    """Ratios these figures imply. Computed here, and labelled as computed.

    Return on equity uses the owners' profit against the owners' equity, because market value is a
    claim on the owners' share. Return on assets uses the whole group's profit against the whole
    group's assets. Mixing those two is the most common way these come out wrong.
    """
    out = []
    for when in sorted(by_year, reverse=True):
        v = by_year[when]["values"]
        owners, group = v.get("net_profit_owners"), v.get("net_profit")
        equity, assets = v.get("equity"), v.get("assets")
        revenue, ebitda, debt = v.get("revenue"), v.get("ebitda"), v.get("debt")
        row = {"year_end": when}
        row["roe_percent"] = round(owners / equity * 100, 1) if owners and equity else None
        row["roa_percent"] = round((group or owners) / assets * 100, 1) if (group or owners) and assets else None
        row["margin_percent"] = round(owners / revenue * 100, 1) if owners and revenue else None
        row["ebitda_margin_percent"] = round(ebitda / revenue * 100, 1) if ebitda and revenue else None
        row["debt_to_equity"] = round(debt / equity, 2) if debt is not None and equity else None
        out.append(row)
    return out


def contradictions(by_year: dict) -> list[str]:
    """Figures that cannot all be right, which is the only check most of these have.

    Four years from one source means no cross-source disagreement is available for anything. What is
    available is arithmetic: a company cannot owe its owners more than everything it has, and profit
    before tax cannot exceed what came in.
    """
    found = []
    for when in sorted(by_year):
        v = by_year[when]["values"]
        if not yahoo.in_rupees(by_year[when]):
            found.append(f"{when}: reported in {by_year[when]['currency']}, not rupees, so it is "
                         f"not comparable with a rupee share price and is not converted")
        assets, equity = v.get("assets"), v.get("equity")
        if assets is not None and equity is not None and equity > assets * 1.001:
            found.append(f"{when}: equity {crore(equity):,} is more than total assets "
                         f"{crore(assets):,}, which cannot be")
        revenue, pbt = v.get("revenue"), v.get("profit_before_tax")
        if revenue is not None and pbt is not None and revenue > 0 and pbt > revenue * 1.5:
            found.append(f"{when}: profit before tax {crore(pbt):,} is more than half again the "
                         f"revenue of {crore(revenue):,}; possible with large other income, but "
                         f"worth reading the accounts before using it")
        for key in ("assets", "revenue"):
            if v.get(key) is not None and v[key] < 0:
                found.append(f"{when}: {key} {crore(v[key]):,} is negative")
    return found


def cross_check(symbol: str, by_year: dict) -> list[str]:
    """Against a second source, when one is configured. Entirely optional.

    This is the one thing a token is worth here, and it is worth a lot: the two sources agree to the
    rupee on most figures, so where they differ by more than a rounding one of them is wrong and
    neither should be relied on.
    """
    try:
        from nsetools import upstox
        access = upstox.token()
    except Exception:                                            # noqa: BLE001  no token is normal
        return []

    try:
        from nsetools import nse
        isin = next((r["isin"] for r in nse.equities()
                     if r.get("symbol", "").upper() == symbol.upper()), None)
        if not isin:
            return []
        other: dict[str, dict] = {}
        for slug, mapping in upstox.STATEMENTS:
            body = upstox.fetch(f"{upstox.BASE}/{isin}/{slug}", access,
                                {"fs": "true", "type": "consolidated"})
            for when, values in upstox.figures(body, mapping, symbol).items():
                other.setdefault(when, {}).update(values)
    except Exception as err:                                     # noqa: BLE001
        return [f"the second source could not be read ({type(err).__name__}), so nothing was "
                f"cross-checked"]

    notes = []
    for when in sorted(set(by_year) & set(other), reverse=True):
        mine, theirs = by_year[when]["values"], other[when]
        for concept in ("revenue", "assets", "profit_before_tax", "cash", "cash_from_operations"):
            a, b = mine.get(concept), theirs.get(concept)
            if a is None or b is None or max(abs(a), abs(b)) == 0:
                continue
            gap = abs(a - b) / max(abs(a), abs(b))
            if gap > 0.01:
                notes.append(f"{when} {concept}: this source {crore(a):,}, the other "
                             f"{crore(b):,}, {gap*100:.1f}% apart")
    if not notes:
        notes.append("the two sources agree to within 1% on every figure they both report")
    return notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--ratios", action="store_true", help="the ratios these figures imply")
    ap.add_argument("--cross-check", action="store_true",
                    help="compare against a second source, if a token happens to be configured")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true", help="also write nse/results.json")
    args = ap.parse_args(argv)

    symbol = args.symbol.upper()
    try:
        by_year = yahoo.accounts(symbol)
    except yahoo.NotReported as err:
        print(f"{err}. Funds, trusts and some recent listings have no accounts here.")
        return 2

    if args.save:
        print(f"wrote {show.hand_off('results.json', by_year)}\n")

    if args.ratios:
        rows = ratios(by_year)
        if not args.json:
            print(f"{symbol}, ratios computed from the reported figures\n")
        show.emit(rows, args.json,
                  [("year_end", "year end", 11), ("roe_percent", "ROE%", 8),
                   ("roa_percent", "ROA%", 8), ("margin_percent", "margin%", 9),
                   ("ebitda_margin_percent", "EBITDA%", 9),
                   ("debt_to_equity", "debt/eq", 9)],
                  empty="Not enough reported to compute anything.",
                  note="Computed here, not reported. ROE uses the owners' profit over the owners' "
                       "equity; ROA uses the group's profit over the group's assets. Mixing those "
                       "is the usual way these come out wrong.")
        return 0

    rows = table_rows(by_year)
    if not args.json:
        print(f"{symbol}, accounts as reported, in crore. {len(rows)} years, which is all this "
              f"source gives.\n")
    show.emit(rows, args.json,
              [("year_end", "year end", 11), ("revenue", "revenue", 10), ("pbt", "pbt", 9),
               ("profit", "profit", 9), ("assets", "assets", 11), ("equity", "equity", 10),
               ("debt", "debt", 9), ("cash", "cash", 9), ("cash_from_ops", "cash ops", 9)],
              empty="Nothing reported.")

    trouble = contradictions(by_year)
    if trouble:
        print("\n   THESE FIGURES CANNOT ALL BE RIGHT:")
        for line in trouble:
            print(f"      {line}")

    if not args.json:
        print("\n   Four of this source's field names mean something other than they say:")
        for concept, warning in sorted(yahoo.WARNED.items()):
            print(f"      {concept:<20} {warning}")
        print("      'profit' above is the owners' share. The share count is today's, so any "
              "per-share\n      figure for a year before the latest bonus is wrong by the bonus "
              "factor.")

    if args.cross_check:
        notes = cross_check(symbol, by_year)
        print("\n   Against a second source:" if notes else "")
        for line in notes:
            print(f"      {line}")
        if not notes:
            print("      No second source configured, which is fine. Nothing here needs one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
