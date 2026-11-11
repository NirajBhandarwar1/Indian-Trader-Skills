#!/usr/bin/env python3
"""Delivery analysis for NSE stocks: separating real buying from intraday churn.

Every exchange publishes volume. NSE also publishes how much of it was taken delivery of, which is
the difference between a stock being traded and a stock being bought. Nothing free outside India
publishes it.

Needs no third-party packages. Run --help for the options.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import nse, show                                # noqa: E402,F401
from nsetools.http import NotPublished                         # noqa: E402

# An illiquid stock that traded four hundred shares all day shows 100% delivery, because the one
# person who bought them kept them. That is arithmetic, not accumulation, so a floor on the day's
# traded value is the difference between this tool being useful and being noise. 1 crore is low
# enough to keep genuine small caps and high enough to drop the untradeable.
MIN_TURNOVER_LACS = 100.0

BANDS = ((70.0, "accumulation, or barely traded"), (40.0, "real participation"),
         (20.0, "ordinary for a liquid large cap"), (0.0, "intraday churn"))


def band(percent: float) -> str:
    for floor, label in BANDS:
        if percent >= floor:
            return label
    return ""


def liquid(rows: list[dict]) -> list[dict]:
    """Companies liquid enough to say anything about. Funds are dropped first.

    An exchange traded fund is held rather than traded, so it sits near 100% delivery permanently.
    Screening the raw bhavcopy for high delivery puts LIQUIDBETF, BANKIETF and MOMENTUM30 at the top
    and not one company, which is how this tool looked before the filter went in.
    """
    return [r for r in nse.companies_only(rows)
            if (r.get("turnover") or 0) >= MIN_TURNOVER_LACS
            and r.get("delivery_percent") is not None
            and (r.get("close") or 0) > 0]


def history(symbol: str, days: int) -> list[dict]:
    """One stock's recent days, newest last. Each day is a separate file, so this costs one call a day."""
    out, day, tried = [], date.today(), 0
    while len(out) < days and tried < days * 2 + 10:
        tried += 1
        if day.weekday() < 5:
            try:
                rows, _which = nse.bhavcopy(day)
            except NotPublished:
                rows = []
            for row in rows:
                if row.get("symbol") == symbol.upper():
                    out.append(row)
                    break
        day -= timedelta(days=1)
    return list(reversed(out))


def spikes(top: int, window: int) -> list[dict]:
    """Stocks whose delivery today is far above their OWN recent normal.

    Compared against itself rather than against other stocks, because a 60% day means something
    different for a stock that usually does 20% than for one that usually does 65%.
    """
    today, rows = nse.last_trading_day()
    now = {r["symbol"]: r for r in liquid(rows)}

    past: dict[str, list[float]] = {}
    day = today - timedelta(days=1)
    seen = 0
    while seen < window and (today - day).days < window * 3:
        if day.weekday() < 5:
            try:
                earlier, _which = nse.bhavcopy(day)
            except NotPublished:
                earlier = []
            if earlier:
                seen += 1
                for row in earlier:
                    pct = row.get("delivery_percent")
                    if pct is not None and row.get("symbol") in now:
                        past.setdefault(row["symbol"], []).append(pct)
        day -= timedelta(days=1)

    found = []
    for symbol, row in now.items():
        was = past.get(symbol, [])
        if len(was) < max(5, window // 2):
            continue                      # too little history to call anything unusual
        normal = statistics.median(was)
        if normal <= 0:
            continue
        pct = row["delivery_percent"]
        found.append({
            "symbol": symbol,
            "date": str(today),
            "delivery_percent": round(pct, 2),
            "normal_percent": round(normal, 2),
            "times_normal": round(pct / normal, 2),
            "close": row["close"],
            "change_percent": round((row["close"] / row["previous_close"] - 1) * 100, 2)
            if row.get("previous_close") else None,
            "turnover_lacs": round(row["turnover"] or 0, 1),
            "reading": band(pct),
        })
    found.sort(key=lambda r: -r["times_normal"])
    return found[:top]


def high_delivery(floor: float, top: int) -> list[dict]:
    today, rows = nse.last_trading_day()
    found = [{
        "symbol": r["symbol"], "date": str(today),
        "delivery_percent": round(r["delivery_percent"], 2),
        "close": r["close"],
        "change_percent": round((r["close"] / r["previous_close"] - 1) * 100, 2)
        if r.get("previous_close") else None,
        "turnover_lacs": round(r["turnover"] or 0, 1),
        "reading": band(r["delivery_percent"]),
    } for r in liquid(rows) if r["delivery_percent"] >= floor]
    found.sort(key=lambda r: -r["turnover_lacs"])
    return found[:top]


def accumulation(top: int, window: int) -> list[dict]:
    """Price going nowhere while delivery climbs: somebody buying without chasing.

    The pattern worth looking for, and the one most easily imagined. Treat it as a list of things to
    read about, not a list of things to buy.
    """
    today, rows = nse.last_trading_day()
    now = {r["symbol"]: r for r in liquid(rows)}
    series: dict[str, list[tuple[float, float]]] = {}
    day, seen = today, 0
    while seen < window and (today - day).days < window * 3:
        if day.weekday() < 5:
            try:
                earlier, _which = nse.bhavcopy(day)
            except NotPublished:
                earlier = []
            if earlier:
                seen += 1
                for row in earlier:
                    pct, close = row.get("delivery_percent"), row.get("close")
                    if pct is not None and close and row.get("symbol") in now:
                        series.setdefault(row["symbol"], []).append((pct, close))
        day -= timedelta(days=1)

    found = []
    for symbol, pairs in series.items():
        if len(pairs) < window - 2:
            continue
        pairs.reverse()                                   # oldest first
        half = len(pairs) // 2
        early_d = statistics.median(p for p, _c in pairs[:half])
        late_d = statistics.median(p for p, _c in pairs[half:])
        early_c = statistics.median(c for _p, c in pairs[:half])
        late_c = statistics.median(c for _p, c in pairs[half:])
        if early_d <= 0 or early_c <= 0:
            continue
        delivery_up = late_d / early_d - 1
        price_move = abs(late_c / early_c - 1)
        if delivery_up > 0.25 and price_move < 0.05:
            found.append({
                "symbol": symbol, "date": str(today),
                "delivery_was": round(early_d, 1), "delivery_now": round(late_d, 1),
                "delivery_up_percent": round(delivery_up * 100, 1),
                "price_moved_percent": round(price_move * 100, 1),
                "close": now[symbol]["close"],
                "turnover_lacs": round(now[symbol]["turnover"] or 0, 1),
            })
    found.sort(key=lambda r: -r["delivery_up_percent"])
    return found[:top]


def print_rows(rows: list[dict], columns: list[tuple[str, str, int]]) -> None:
    if not rows:
        print("Nothing matched.")
        return
    print("  ".join(f"{title:<{w}}" for _k, title, w in columns))
    print("  ".join("-" * w for _k, _t, w in columns))
    for row in rows:
        cells = []
        for key, _title, w in columns:
            v = row.get(key)
            cells.append(f"{v:<{w}}" if isinstance(v, str) else
                         (f"{v:<{w}}" if v is None else f"{v:<{w}}"))
        print("  ".join(cells))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spikes", action="store_true", help="delivery far above the stock's own norm")
    ap.add_argument("--high", type=show.number, metavar="PCT", help="stocks closing above PCT delivery")
    ap.add_argument("--accumulation", action="store_true",
                    help="price flat while delivery climbs")
    ap.add_argument("--symbol", help="one stock's delivery history")
    ap.add_argument("--days", type=int, default=20, help="how many days of history (default 20)")
    ap.add_argument("--top", type=int, default=25, help="how many rows (default 25)")
    ap.add_argument("--json", action="store_true", help="machine readable, for another skill")
    args = ap.parse_args(argv)

    try:
        if args.symbol:
            rows = history(args.symbol, args.days)
            out = [{"date": r["date"], "close": r["close"],
                    "delivery_percent": round(r["delivery_percent"], 2)
                    if r.get("delivery_percent") is not None else None,
                    "volume": r.get("volume"), "reading": band(r.get("delivery_percent") or 0)}
                   for r in rows]
            if args.json:
                print(json.dumps(out, indent=2))
            else:
                print(f"{args.symbol.upper()}, last {len(out)} published days\n")
                print_rows(out, [("date", "date", 12), ("close", "close", 10),
                           ("delivery_percent", "delivery%", 10), ("reading", "reading", 34)])
                got = [r["delivery_percent"] for r in out if r["delivery_percent"] is not None]
                if got:
                    print(f"\nmedian delivery over the period: {statistics.median(got):.1f}%")
            return 0

        if args.spikes:
            rows = spikes(args.top, args.days)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print(f"Delivery today against each stock's own {args.days}-day median\n")
                print_rows(rows, [("symbol", "symbol", 12), ("delivery_percent", "today%", 8),
                            ("normal_percent", "usual%", 8), ("times_normal", "x usual", 8),
                            ("change_percent", "price%", 8), ("turnover_lacs", "turnover", 11),
                            ("reading", "reading", 30)])
                print("\nA spike can be one block deal. Cross-check with nse-insider.")
            return 0

        if args.high is not None:
            rows = high_delivery(args.high, args.top)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print(f"Stocks closing above {args.high:.0f}% delivery, busiest first\n")
                print_rows(rows, [("symbol", "symbol", 12), ("delivery_percent", "delivery%", 10),
                            ("close", "close", 10), ("change_percent", "price%", 8),
                            ("turnover_lacs", "turnover", 11), ("reading", "reading", 30)])
            return 0

        if args.accumulation:
            rows = accumulation(args.top, args.days)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print(f"Price flat, delivery climbing, over {args.days} days\n")
                print_rows(rows, [("symbol", "symbol", 12), ("delivery_was", "was%", 7),
                            ("delivery_now", "now%", 7), ("delivery_up_percent", "up%", 7),
                            ("price_moved_percent", "price%", 8), ("close", "close", 10),
                            ("turnover_lacs", "turnover", 11)])
                print("\nThings to read about, not things to buy.")
            return 0

        ap.print_help()
        return 1

    except NotPublished as err:
        print(f"The exchange has nothing to give yet: {err}")
        print("Delivery is published after the close, around 7pm India time.")
        return 2


if __name__ == "__main__":
    show.run(main)
