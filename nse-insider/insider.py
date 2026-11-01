#!/usr/bin/env python3
"""Who is actually buying: insiders, institutions in bulk, and the daily foreign/domestic flow.

Three disclosures the exchange publishes and the US has no equal of:

  insider trades   filed under the PIT regulations within two trading days of the transaction,
                   naming the person, their category, the mode and the value
  bulk deals       any single party trading more than 0.5% of a company's shares in a day, naming
                   the client and the average price they paid
  FII / DII flows  what foreign and domestic institutions bought and sold across the whole market

Needs no third-party packages and no account.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nsetools import nse, show                                 # noqa: E402
from nsetools.http import NotPublished                          # noqa: E402

CRORE = 1e7

# A promoter buying in the open market is a different signal from an employee trust settling ESOPs,
# and the mode says which. Kept as a label rather than a score: ranking these would be inventing a
# judgement the filing does not support.
TELLING = {"market purchase", "market sale", "open market", "off market"}


def rupees(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def day_of(raw: str) -> str:
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw).strip()[:17], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return str(raw)[:10]


def insiders(symbol: str, days: int, min_crore: float) -> list[dict]:
    """Insider and promoter dealings for one company.

    A SYMBOL IS REQUIRED. Asked market-wide the endpoint answers 200 with an empty list however
    wide the dates, which reads as "no insider activity anywhere" and is wrong -- INFY returns 197
    filings over a year. Measured 2026-10-23.
    """
    out = []
    for row in nse.insider_trades(symbol, days):
        value = rupees(row.get("secVal"))
        if value is not None and min_crore and value < min_crore * CRORE:
            continue
        mode = (row.get("acqMode") or "").strip()
        before = rupees(row.get("befAcqSharesPer"))
        after = rupees(row.get("afterAcqSharesPer"))
        out.append({
            "filed": day_of(row.get("date") or ""),
            "traded": day_of(row.get("acqfromDt") or ""),
            "symbol": (row.get("symbol") or symbol).upper(),
            "who": (row.get("acqName") or "").strip(),
            "category": (row.get("personCategory") or "").strip(),
            "mode": mode,
            "security": (row.get("secType") or "").strip(),
            "shares": rupees(row.get("secAcq")),
            "value_crore": round(value / CRORE, 3) if value is not None else None,
            "holding_before_percent": before,
            "holding_after_percent": after,
            "moved_percent": round(after - before, 3)
                             if before is not None and after is not None else None,
            "telling": mode.lower() in TELLING,
        })
    out.sort(key=lambda r: (r["filed"], r["value_crore"] or 0), reverse=True)
    return out


def deals(kind: str, days: int, symbol: str | None, min_crore: float) -> list[dict]:
    """Bulk deals, block deals or short selling, with the counterparty named."""
    out = []
    for row in nse.bulk_block_deals(kind, days):
        sym = (row.get("BD_SYMBOL") or "").upper()
        if symbol and sym != symbol.upper():
            continue
        qty = rupees(row.get("BD_QTY_TRD"))
        price = rupees(row.get("BD_TP_WATP"))
        value = (qty or 0) * (price or 0)
        if min_crore and value < min_crore * CRORE:
            continue
        out.append({
            "date": day_of(row.get("BD_DT_DATE") or ""),
            "symbol": sym,
            "company": (row.get("BD_SCRIP_NAME") or "").strip(),
            "client": (row.get("BD_CLIENT_NAME") or "").strip(),
            "side": (row.get("BD_BUY_SELL") or "").strip().upper(),
            "shares": qty,
            "price": price,
            "value_crore": round(value / CRORE, 2),
        })
    mark_round_trips(out)
    out.sort(key=lambda r: (r["date"], r["value_crore"]), reverse=True)
    return out


def mark_round_trips(rows: list[dict]) -> None:
    """Flag a client who both bought and sold the same stock on the same day.

    This is the trap in bulk deal data and it is not small. Measured on 2026-10-19, the three
    largest disclosed deals of the week were ALPHAGREP buying 758,066 JUSTDIAL at 764.24 and selling
    758,066 at 764.48, and MICROCURVES doing the same with 3,445,311 IXIGO. A desk passing stock
    through itself crosses the 0.5% disclosure threshold on both legs and shows up twice, so a naive
    reading sees 58 deals of real interest where most of the top of the list is one firm's inventory
    going round.

    Nothing is dropped, because the deal did happen and the price is real. It is labelled, so a
    reader can tell a position being built from a book being turned.
    """
    by_day: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in rows:
        key = (row["date"], row["symbol"], row["client"])
        by_day.setdefault(key, {})[row["side"]] = (
            by_day.setdefault(key, {}).get(row["side"], 0.0) + (row["shares"] or 0.0))

    for row in rows:
        sides = by_day.get((row["date"], row["symbol"], row["client"]), {})
        bought, sold = sides.get("BUY", 0.0), sides.get("SELL", 0.0)
        both = bought > 0 and sold > 0
        # BOTH SIDES is the signal, not how flat the legs are. A 10% band was tried first and let
        # IRAGE through on CMLL -- 33.70 crore bought and 28.73 crore sold the same day, 15% apart,
        # which read as a real position and is a desk working an order. Any client on both sides of
        # the same stock on the same day is turning stock, whatever the residue.
        row["round_trip"] = bool(both)
        row["net_shares"] = round(bought - sold, 0) if both else row["shares"]
        if not both:
            row["note"] = ""
        elif abs(bought - sold) <= max(bought, sold) * 0.1:
            row["note"] = "both sides, flat by the close"
        else:
            row["note"] = f"both sides, net {bought - sold:+,.0f}"


def flows() -> list[dict]:
    """What foreign and domestic institutions did across the market, in crore."""
    out = []
    for row in nse.fii_dii_flows():
        out.append({
            "date": day_of(row.get("date") or ""),
            "who": (row.get("category") or "").strip(),
            "bought_crore": rupees(row.get("buyValue")),
            "sold_crore": rupees(row.get("sellValue")),
            "net_crore": rupees(row.get("netValue")),
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", help="one company, required for --insiders")
    ap.add_argument("--insiders", action="store_true", help="PIT filings for a company")
    ap.add_argument("--bulk", action="store_true", help="bulk deals, over 0.5% of a company in a day")
    ap.add_argument("--block", action="store_true", help="block deals, single negotiated trades")
    ap.add_argument("--short", action="store_true", help="short selling disclosures")
    ap.add_argument("--flows", action="store_true", help="foreign and domestic institutional flow")
    ap.add_argument("--days", type=int, default=30, help="how far back (default 30)")
    ap.add_argument("--min-crore", type=float, default=0.0, help="ignore anything smaller")
    ap.add_argument("--real-only", action="store_true",
                    help="hide deals where the same client was flat by the close")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true", help="also write nse/insider.json")
    args = ap.parse_args(argv)

    try:
        if args.insiders:
            if not args.symbol:
                print("--insiders needs --symbol. Asked market-wide the exchange returns an empty")
                print("list however wide the dates, which looks like no activity and is not.")
                return 1
            rows = insiders(args.symbol, args.days, args.min_crore)
            if args.save:
                print(f"wrote {show.hand_off('insider.json', rows)}\n")
            if not args.json:
                print(f"{args.symbol.upper()} insider and promoter filings, last {args.days} days. "
                      f"{len(rows)} found.\n")
            show.emit(rows[:args.top], args.json,
                      [("filed", "filed", 11), ("traded", "traded", 11), ("who", "who", 30),
                       ("category", "category", 12), ("mode", "mode", 16),
                       ("value_crore", "cr", 9), ("moved_percent", "moved%", 8)],
                      empty="No filings in that window. Try a wider --days; a year often has some.",
                      note="An ESOP settlement is not a promoter buying in the market. Read the "
                           "mode column before concluding anything.")
            return 0

        for flag, kind, label in ((args.bulk, "bulk_deals", "Bulk deals"),
                                  (args.block, "block_deals", "Block deals"),
                                  (args.short, "short_selling", "Short selling")):
            if not flag:
                continue
            rows = deals(kind, args.days, args.symbol, args.min_crore)
            if args.save:
                print(f"wrote {show.hand_off('insider.json', rows)}\n")
            churn = sum(1 for r in rows if r.get("round_trip"))
            if args.real_only:
                rows = [r for r in rows if not r.get("round_trip")]
            if not args.json:
                where = f" in {args.symbol.upper()}" if args.symbol else ""
                print(f"{label}{where}, last {args.days} days. {len(rows)} disclosed"
                      + (f", {churn} two-sided hidden" if args.real_only and churn
                         else f", {churn} of them two-sided" if churn else "")
                      + ".\n")
            show.emit(rows[:args.top], args.json,
                      [("date", "date", 11), ("symbol", "symbol", 12), ("side", "side", 5),
                       ("client", "who", 34), ("shares", "shares", 11),
                       ("price", "price", 9), ("value_crore", "cr", 7),
                       ("note", "note", 30)],
                      empty="Nothing disclosed in that window.",
                      note=("A client on both sides of the same stock on the same day is turning "
                            "stock, not building a position. --real-only drops them." if churn else
                            "A named client is one decision. The same volume spread over a "
                            "thousand accounts is not."))
            return 0

        if args.flows:
            rows = flows()
            if not args.json:
                print("Institutional flow across the whole market, in crore.\n")
            show.emit(rows, args.json,
                      [("date", "date", 11), ("who", "who", 10), ("bought_crore", "bought", 12),
                       ("sold_crore", "sold", 12), ("net_crore", "net", 12)],
                      empty="The exchange has not published today's flow yet.",
                      note="A market-wide figure says nothing about any one stock.")
            return 0

        ap.print_help()
        return 1

    except NotPublished as err:
        print(f"The exchange gave nothing: {err}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
