#!/usr/bin/env python3
"""Replay a setup over the market's own history and report what it actually did.

The only skill here that can tell you a rule is worthless. Every other one reports what the
exchange published; this one takes a rule, runs it over every stock and every day it has, and gives
you the numbers -- including the number that matters most, which is what buying everything would
have done over the same days.

A setup that wins 55% of the time is a bad setup if the market rose and everything won 60%. Most
backtests never print that comparison, which is how they flatter a rule into looking like an edge.

Standard library only. The exchange is asked once per trading day, ever, and cached.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import nse, show                                  # noqa: E402
from nsetools.http import NotPublished                           # noqa: E402
from nsetools.store import Prices, missing_days                  # noqa: E402

# The setups, the liquidity floor and the cost figure live in nsetools.setups, so /nse-scan screens
# exactly the rule this measures. They are re-exported here because callers and tests read them as
# backtest.SETUPS and backtest.MIN_TURNOVER_LACS.
from nsetools.setups import (COSTS_ROUND_TRIP_PERCENT, MIN_TURNOVER_LACS,  # noqa: E402,F401
                             SETUPS, delivery_and_breakout, delivery_spike, oversold,
                             oversold_bounce, plain_breakout, quiet_accumulation)


# ---- the replay --------------------------------------------------------------------------------

def run(store: Prices, setup: str, hold: int, stop_percent: float, target_percent: float | None,
        window: int) -> dict:
    """Every time the rule fired, what happened next. Entry is the NEXT day's open."""
    rule = SETUPS[setup]
    days = store.trading_days()
    if len(days) < window + hold + 2:
        raise SystemExit(f"only {len(days)} trading days cached; this needs at least "
                         f"{window + hold + 2}. Run --fetch first.")

    symbols = [r[0] for r in store.db.execute(
        "SELECT symbol FROM bars GROUP BY symbol HAVING COUNT(*) >= ?", (window + hold + 2,))]

    trades, baseline = [], []
    for symbol in symbols:
        bars = store.series(symbol)
        by_day = {b["traded_on"]: b for b in bars}
        if len(bars) < window + hold + 2:
            continue
        for i in range(window, len(bars) - hold - 1):
            today = bars[i]
            if (today.get("turnover_lacs") or 0) < MIN_TURNOVER_LACS:
                continue

            # The baseline: what holding this stock for the same period would have done, from every
            # day it was tradeable. This is what the setup has to beat.
            entry_bar = bars[i + 1]
            exit_bar = bars[i + 1 + hold]
            if entry_bar.get("open") and exit_bar.get("close"):
                baseline.append((exit_bar["close"] / entry_bar["open"] - 1) * 100
                                - COSTS_ROUND_TRIP_PERCENT)

            if not rule(bars, i, window):
                continue
            entry = entry_bar.get("open")
            if not entry:
                continue

            stop = entry * (1 - stop_percent / 100)
            target = entry * (1 + target_percent / 100) if target_percent else None
            got, why = None, "held"
            for ahead in bars[i + 1:i + 1 + hold + 1]:
                low, high, opened = ahead.get("low"), ahead.get("high"), ahead.get("open")
                # The stop is checked before the target. Within one daily bar there is no way to
                # know which came first, so the pessimistic order is the honest one: a backtest
                # that takes the target when both were touched invents money.
                if low is not None and low <= stop:
                    # A GAP THROUGH THE STOP FILLS AT THE OPEN, NOT AT THE STOP. This is the single
                    # most flattering assumption a backtest can make, and the first version of this
                    # file made it: every setup reported a worst trade of exactly -5.29%, the stop
                    # plus costs, which cannot happen. A stock that closes at 100 and opens at 88
                    # does not fill your 95 stop at 95. Overnight gaps are where real losses live
                    # and pretending otherwise turns an honest rule into a flattering one.
                    got = min(stop, opened) if opened is not None else stop
                    why = "gapped" if opened is not None and opened < stop else "stopped"
                    break
                if target and high is not None and high >= target:
                    # The same logic the other way, and it does NOT favour you: a gap up through
                    # the target fills above it, so the target is taken at the better of the two.
                    got = max(target, opened) if opened is not None else target
                    why = "target"
                    break
            if got is None:
                got = bars[i + 1 + hold].get("close")
                if not got:
                    continue

            trades.append({
                "symbol": symbol, "signal_on": today["traded_on"],
                "entry_on": entry_bar["traded_on"], "entry": round(entry, 2),
                "exit": round(got, 2), "why": why,
                "return_percent": round((got / entry - 1) * 100 - COSTS_ROUND_TRIP_PERCENT, 2),
                "delivery_percent": today.get("delivery_percent"),
            })

    return {"trades": trades, "baseline": baseline, "days": len(days),
            "symbols": len(symbols), "first": days[0], "last": days[-1]}


def summarise(out: dict, setup: str, hold: int, stop: float, target: float | None) -> dict:
    trades = out["trades"]
    rets = [t["return_percent"] for t in trades]
    base = out["baseline"]
    if not rets:
        return {"setup": setup, "trades": 0}

    wins = [r for r in rets if r > 0]
    summary = {
        "setup": setup, "hold_days": hold, "stop_percent": stop, "target_percent": target,
        "window": f"{out['first']} to {out['last']}",
        "trading_days": out["days"], "symbols": out["symbols"],
        "trades": len(rets),
        "win_rate": round(len(wins) / len(rets) * 100, 1),
        "average_percent": round(statistics.mean(rets), 2),
        "median_percent": round(statistics.median(rets), 2),
        "best_percent": round(max(rets), 2),
        "worst_percent": round(min(rets), 2),
        "stopped_out": sum(1 for t in trades if t["why"] in ("stopped", "gapped")),
        "gapped_through": sum(1 for t in trades if t["why"] == "gapped"),
        "worse_than_stop": sum(1 for t in trades
                               if t["return_percent"] < -(stop + COSTS_ROUND_TRIP_PERCENT)),
        "hit_target": sum(1 for t in trades if t["why"] == "target"),
        "total_percent": round(sum(rets), 1),
    }
    if base:
        base_wins = [r for r in base if r > 0]
        summary["baseline_win_rate"] = round(len(base_wins) / len(base) * 100, 1)
        summary["baseline_average_percent"] = round(statistics.mean(base), 2)
        summary["edge_over_baseline"] = round(summary["average_percent"]
                                              - summary["baseline_average_percent"], 2)
        summary["baseline_samples"] = len(base)
    return summary


def report(s: dict) -> None:
    if not s.get("trades"):
        print("The rule never fired on the days cached. Try a longer --fetch or a looser setup.")
        return
    print(f"{s['setup']}, holding {s['hold_days']} days, stop {s['stop_percent']}%"
          + (f", target {s['target_percent']}%" if s.get("target_percent") else "")
          + f"\n{s['window']}, {s['trading_days']} trading days, {s['symbols']:,} symbols\n")
    print(f"   trades                  {s['trades']:,}")
    print(f"   win rate                {s['win_rate']}%")
    print(f"   average return          {s['average_percent']:+.2f}%   (after 0.293% costs)")
    print(f"   median return           {s['median_percent']:+.2f}%")
    print(f"   best / worst            {s['best_percent']:+.2f}% / {s['worst_percent']:+.2f}%")
    print(f"   stopped out / target    {s['stopped_out']:,} / {s['hit_target']:,}")
    if s.get("gapped_through"):
        print(f"   gapped THROUGH the stop {s['gapped_through']:,}  "
              f"(filled at the open, worse than the stop)")

    if "baseline_average_percent" in s:
        print(f"\n   BUYING EVERYTHING over the same days, held the same length:")
        print(f"   baseline win rate       {s['baseline_win_rate']}%")
        print(f"   baseline average        {s['baseline_average_percent']:+.2f}%   "
              f"({s['baseline_samples']:,} samples)")
        edge = s["edge_over_baseline"]
        print(f"\n   EDGE                    {edge:+.2f} percentage points")
        if edge <= 0:
            print("   The setup did worse than buying anything at random. It is not an edge.")
        elif s["trades"] < 30:
            print(f"   On {s['trades']} trades this is not evidence of anything. "
                  f"Cache more history.")
        else:
            print("   Positive, on this sample, over this window. One window is not a strategy.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", type=int, metavar="DAYS",
                    help="cache this many weekdays of the whole market, then stop")
    ap.add_argument("--setup", choices=sorted(SETUPS), help="which rule to replay")
    ap.add_argument("--hold", type=int, default=10, help="trading days to hold (default 10)")
    ap.add_argument("--stop", type=show.number, default=5.0, help="stop, percent below entry (default 5)")
    ap.add_argument("--target", type=show.number, help="optional target, percent above entry")
    ap.add_argument("--window", type=int, default=20, help="lookback the rule uses (default 20)")
    ap.add_argument("--db", default="nse/prices.sqlite")
    ap.add_argument("--trades", action="store_true", help="list every trade")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    store = Prices(args.db)
    try:
        if args.fetch:
            wanted = missing_days(store, args.fetch)
            if not wanted:
                print("Nothing to fetch; every weekday in that window is already cached.")
            got = 0
            for day in wanted:
                try:
                    rows, which = nse.bhavcopy(day)
                    rows = nse.companies_only(rows)
                    new = store.put_day(day, rows, which)
                    got += 1
                    print(f"   {day}  {len(rows):>5,} companies  {new:>5,} new", flush=True)
                except NotPublished:
                    store.put_day(day, [], "not published")
                    print(f"   {day}  nothing published (holiday, or too recent)", flush=True)
            c = store.counts()
            print(f"\ncache: {c['days']} trading days, {c['bars']:,} bars, {c['symbols']:,} "
                  f"symbols, {c['first']} to {c['last']}, {c['megabytes']} MB")
            return 0

        if not args.setup:
            c = store.counts()
            print(f"cache: {c['days']} trading days, {c['bars']:,} bars, {c['symbols'] or 0:,} "
                  f"symbols" + (f", {c['first']} to {c['last']}" if c['first'] else "") + "\n")
            ap.print_help()
            return 1

        out = run(store, args.setup, args.hold, args.stop, args.target, args.window)
        s = summarise(out, args.setup, args.hold, args.stop, args.target)
        # Kept so a screen can print this rule's record beside the stocks it finds. A screener that
        # shows candidates without the rule's history invites acting on a rule nobody checked.
        if s.get("trades"):
            store.remember(s)
        if args.json:
            import json
            print(json.dumps({"summary": s, "trades": out["trades"]} if args.trades
                             else {"summary": s}, indent=2))
            return 0
        report(s)
        if args.trades and out["trades"]:
            print()
            best = sorted(out["trades"], key=lambda t: -t["return_percent"])
            show.table(best[:20], [("signal_on", "signal", 11), ("symbol", "symbol", 12),
                                   ("entry", "entry", 10), ("exit", "exit", 10),
                                   ("why", "why", 9), ("return_percent", "return%", 9)])
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    show.run(main)
