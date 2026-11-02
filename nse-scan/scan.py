#!/usr/bin/env python3
"""Scan the whole market for setups firing today, and show each rule's record beside them.

A screener that lists candidates without the rule's history invites you to act on a rule nobody
checked. So every result here carries what nse-backtest measured for that rule: the trade count,
the average return, and the edge over simply buying anything. Where a rule has a negative edge the
candidates are still listed and the header says plainly that the rule lost money.

Reads the local price cache, so a scan is offline once --fetch has been run.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nsetools import show                                        # noqa: E402
from nsetools.store import Prices                                 # noqa: E402

# The setups live with the replay rather than being duplicated here, so a rule can never be screened
# on one definition and measured on another.
_spec = importlib.util.spec_from_file_location("backtest", ROOT / "nse-backtest" / "backtest.py")
_bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bt)
SETUPS, MIN_TURNOVER_LACS = _bt.SETUPS, _bt.MIN_TURNOVER_LACS

# How many trades before a win rate means anything. See record_line.
MEANINGFUL_TRADES = 100


def firing(store: Prices, setup: str, window: int, on: str | None = None) -> list[dict]:
    """Every stock the rule fires on, for the latest cached day or one you name."""
    rule = SETUPS[setup]
    days = store.trading_days()
    if not days:
        raise SystemExit("No cached days. Run: python nse-backtest/backtest.py --fetch 250")
    day = on or days[-1]
    if day not in days:
        raise SystemExit(f"{day} is not in the cache. Cached: {days[0]} to {days[-1]}")

    symbols = [r[0] for r in store.db.execute(
        "SELECT symbol FROM bars GROUP BY symbol HAVING COUNT(*) > ?", (window,))]
    found = []
    for symbol in symbols:
        bars = store.series(symbol)
        where = next((n for n, b in enumerate(bars) if b["traded_on"] == day), None)
        if where is None or where < window:
            continue
        today = bars[where]
        if (today.get("turnover_lacs") or 0) < MIN_TURNOVER_LACS:
            continue
        if not rule(bars, where, window):
            continue
        prev = today.get("previous_close")
        found.append({
            "symbol": symbol,
            "close": today.get("close"),
            "change_percent": round((today["close"] / prev - 1) * 100, 2)
            if prev and today.get("close") else None,
            "delivery_percent": round(today["delivery_percent"], 1)
            if today.get("delivery_percent") is not None else None,
            "turnover_lacs": round(today.get("turnover_lacs") or 0, 1),
        })
    found.sort(key=lambda r: -(r["turnover_lacs"] or 0))
    return found


def record_line(record: dict | None, setup: str) -> tuple[str, bool]:
    """What this rule has actually done, and whether it is worth acting on."""
    if not record or not record.get("trades"):
        return (f"This rule has NOT been measured. Run: "
                f"python nse-backtest/backtest.py --setup {setup}"), False
    edge = record.get("edge")
    line = (f"measured: {record['trades']:,} trades, {record['win_rate']}% win rate, "
            f"{record['average']:+.2f}% average against a baseline of {record['baseline']:+.2f}%, "
            f"edge {edge:+.2f}pp  ({record['window']})")
    if edge is None or edge <= 0:
        return line + "\n   THIS RULE LOST TO BUYING AT RANDOM over the window measured.", False
    # A hundred, not thirty. Thirty is the rule of thumb for a mean behaving normally; it is far too
    # few to claim a win rate from. Pinning a win rate to within about ten points at any useful
    # confidence takes on the order of a hundred trades, and below that the number moves several
    # points if one trade goes the other way. The shipped delivery-spike rule has 57 and is
    # therefore not evidence, which is the honest reading of it.
    if record["trades"] < MEANINGFUL_TRADES:
        return line + (f"\n   Only {record['trades']} trades measured, under {MEANINGFUL_TRADES}: "
                       f"not enough to claim a win rate from."), False
    if record.get("median") is not None and record["median"] <= -record["stop_percent"]:
        return line + ("\n   The median trade hit the stop: more than half lose, and the average "
                       "is carried by a few outliers."), False
    return line, True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", choices=sorted(SETUPS), help="one rule")
    ap.add_argument("--all", action="store_true", help="every rule, worst record last")
    ap.add_argument("--window", type=int, default=20, help="the lookback the rule uses")
    ap.add_argument("--on", help="scan a past cached day instead of the latest, YYYY-MM-DD")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--db", default="nse/prices.sqlite")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true", help="also write nse/scan.json")
    args = ap.parse_args(argv)

    store = Prices(args.db)
    try:
        wanted = sorted(SETUPS) if args.all else ([args.setup] if args.setup else [])
        if not wanted:
            c = store.counts()
            print(f"cache: {c['days']} trading days"
                  + (f", {c['first']} to {c['last']}" if c["first"] else " (empty)") + "\n")
            ap.print_help()
            return 1

        if args.all:
            # Best record first, so the rule most worth reading is at the top and the ones that
            # lost money are at the bottom where they belong.
            wanted.sort(key=lambda s: -((store.record_for(s) or {}).get("edge") or -99))

        everything = {}
        for setup in wanted:
            rows = firing(store, setup, args.window, args.on)
            record = store.record_for(setup)
            note, trustworthy = record_line(record, setup)
            everything[setup] = {"candidates": rows, "record": record,
                                 "worth_acting_on": trustworthy}
            if args.json:
                continue
            print(f"=== {setup} ===")
            print(f"   {note}\n")
            show.table(rows[:args.top],
                       [("symbol", "symbol", 12), ("close", "close", 10),
                        ("change_percent", "change%", 9), ("delivery_percent", "delivery%", 10),
                        ("turnover_lacs", "turnover", 11)],
                       empty="   Nothing fired today.")
            if len(rows) > args.top:
                print(f"   ... and {len(rows) - args.top} more")
            print()

        if args.save:
            print(f"wrote {show.hand_off('scan.json', everything)}")
        if args.json:
            import json
            print(json.dumps(everything, indent=2, default=str))
            return 0

        usable = [s for s, v in everything.items() if v["worth_acting_on"]]
        if not usable:
            print("None of these rules has a record worth acting on. A candidate list is not a "
                  "reason to buy.")
        print("Nothing here decides anything. Size with /nse-risk, which can say no.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
