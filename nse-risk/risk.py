#!/usr/bin/env python3
"""Position sizing that answers PASS or BLOCK, and shows the arithmetic either way.

The only skill here that can say no. Everything else reports what the exchange published; this one
takes a price, a stop and your capital and works out how many shares that allows -- or refuses.

Needs no network and no account. The numbers come from you.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nsetools import show                                      # noqa: E402

DEFAULTS = {"capital": 100000.0, "risk_per_trade_percent": 1.0, "max_positions": 5}

# India-specific costs, as of 2026. Rounded up rather than down: a sizing tool that flatters the
# arithmetic is worse than none. These are for a delivery trade on NSE equity; intraday rates differ
# and a discount broker's brokerage may be lower, which only makes the real figure better than this.
COSTS = {
    "brokerage_percent": 0.03,        # a flat-fee broker is cheaper; this is a safe upper bound
    "stt_percent": 0.1,               # securities transaction tax, delivery, on both sides
    "exchange_percent": 0.00325,      # NSE transaction charge
    "gst_percent": 18.0,              # on brokerage plus exchange charges
    "sebi_percent": 0.0001,
    "stamp_percent": 0.015,           # on buy only
}

# A stop closer than this to the entry is usually noise rather than a level: on an Indian mid cap a
# 0.5% band is inside one minute's range, so the position it implies is enormous and the stop will
# be taken out by nothing at all.
MIN_STOP_PERCENT = 1.0
# And a stop further than this means the idea is a different size of bet than this tool assumes.
MAX_STOP_PERCENT = 25.0


def settings(path: Path | None) -> dict:
    out = dict(DEFAULTS)
    for candidate in ([path] if path else [Path.cwd() / "config.json",
                                           Path(__file__).resolve().parent.parent / "config.json"]):
        if candidate and candidate.exists():
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for key in DEFAULTS:
                if isinstance(raw.get(key), (int, float)):
                    out[key] = float(raw[key])
            out["_from"] = str(candidate)
            break
    return out


def costs(value: float, buying: bool) -> dict:
    """What the trade costs before it has done anything, in rupees."""
    brokerage = value * COSTS["brokerage_percent"] / 100
    stt = value * COSTS["stt_percent"] / 100
    exchange = value * COSTS["exchange_percent"] / 100
    sebi = value * COSTS["sebi_percent"] / 100
    stamp = value * COSTS["stamp_percent"] / 100 if buying else 0.0
    gst = (brokerage + exchange) * COSTS["gst_percent"] / 100
    total = brokerage + stt + exchange + sebi + stamp + gst
    return {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi,
            "stamp": stamp, "gst": gst, "total": total}


def size(entry: float, stop: float, conf: dict, target: float | None = None,
         open_positions: int = 0) -> dict:
    """How many shares, or why none. Every refusal says what would have to change."""
    capital = conf["capital"]
    risk_percent = conf["risk_per_trade_percent"]
    blocks: list[str] = []
    notes: list[str] = []

    if entry <= 0 or stop <= 0:
        blocks.append("entry and stop must both be positive")
    if stop >= entry:
        blocks.append(f"stop {stop:,.2f} is at or above entry {entry:,.2f}; "
                      f"this tool sizes long trades only")

    stop_percent = (entry - stop) / entry * 100 if entry > 0 and stop < entry else 0.0
    if 0 < stop_percent < MIN_STOP_PERCENT:
        blocks.append(f"stop is {stop_percent:.2f}% away, closer than {MIN_STOP_PERCENT}%: "
                      f"inside one minute's range on most Indian mid caps, so it would be taken "
                      f"out by noise and the size it implies is unreal")
    if stop_percent > MAX_STOP_PERCENT:
        blocks.append(f"stop is {stop_percent:.1f}% away, beyond {MAX_STOP_PERCENT}%: "
                      f"that is a different size of bet than this tool assumes")

    if open_positions >= conf["max_positions"]:
        blocks.append(f"{open_positions} positions already open, limit is "
                      f"{int(conf['max_positions'])}")

    risk_rupees = capital * risk_percent / 100
    per_share_risk = max(entry - stop, 0.0)
    shares = int(risk_rupees / per_share_risk) if per_share_risk > 0 else 0

    value = shares * entry
    if value > capital:
        shares = int(capital / entry) if entry > 0 else 0
        value = shares * entry
        notes.append("size cut to fit capital; the stop allowed more than you have")

    if shares <= 0 and not blocks:
        blocks.append(f"one share costs {entry:,.2f} and the risk budget is "
                      f"{risk_rupees:,.2f}; the smallest position already risks more than allowed")

    buy = costs(value, buying=True)
    sell_at_stop = costs(shares * stop, buying=False)
    round_trip = buy["total"] + sell_at_stop["total"]
    loss_at_stop = shares * per_share_risk + round_trip

    # Costs are not a footnote. On a tight stop they are a material share of what you are risking.
    #
    # The threshold is arithmetic rather than taste. The round trip is about 0.293% of the position
    # (0.154% buying, 0.139% selling), and the risk budget is shares times the stop distance, so
    # costs against budget comes to roughly 0.00293 x entry / distance -- it depends only on how
    # tight the stop is, not on the capital or the size.
    #
    #   stop 1% away  -> 29% of the budget      stop 3% away  -> 10%
    #   stop 2% away  -> 15%                    stop 5% away  ->  6%
    #
    # This was first written as a third of the budget, which a test showed could never happen: a
    # third needs a stop inside 0.89%, and anything under 1% is blocked above. The check was dead.
    # 15% fires for a stop tighter than about 2%, which is the band worth a word.
    if shares > 0 and round_trip > risk_rupees * 0.15:
        notes.append(f"costs are {round_trip/risk_rupees*100:.0f}% of the risk budget, because the "
                     f"stop is {stop_percent:.1f}% away; a wider stop carries them better")

    out = {
        "verdict": "BLOCK" if blocks else "PASS",
        "shares": shares if not blocks else 0,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "stop_percent": round(stop_percent, 2),
        "position_value": round(value, 2) if not blocks else 0.0,
        "capital": round(capital, 2),
        "position_percent_of_capital": round(value / capital * 100, 1) if capital and not blocks else 0.0,
        "risk_budget": round(risk_rupees, 2),
        "risk_per_share": round(per_share_risk, 2),
        "costs_round_trip": round(round_trip, 2) if not blocks else 0.0,
        "loss_if_stopped": round(loss_at_stop, 2) if not blocks else 0.0,
        "reasons": blocks,
        "notes": notes,
    }

    if target and target > entry and not blocks:
        gain = shares * (target - entry) - buy["total"] - costs(shares * target, False)["total"]
        out["target"] = round(target, 2)
        out["gain_if_target"] = round(gain, 2)
        out["reward_to_risk"] = round(gain / loss_at_stop, 2) if loss_at_stop > 0 else None
        if out["reward_to_risk"] is not None and out["reward_to_risk"] < 1.5:
            out["notes"].append(f"reward to risk is {out['reward_to_risk']}, under 1.5 after costs; "
                                f"you need to be right more often than not for this to pay")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entry", type=float, required=True, help="the price you would pay")
    ap.add_argument("--stop", type=float, required=True, help="where you would admit you were wrong")
    ap.add_argument("--target", type=float, help="optional, to get reward against risk")
    ap.add_argument("--capital", type=float, help="overrides config.json")
    ap.add_argument("--risk", type=float, help="percent of capital at risk, overrides config.json")
    ap.add_argument("--open-positions", type=int, default=0, help="how many you already hold")
    ap.add_argument("--config", type=Path, help="a config file to read instead of the usual places")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    conf = settings(args.config)
    if args.capital:
        conf["capital"] = args.capital
    if args.risk:
        conf["risk_per_trade_percent"] = args.risk

    out = size(args.entry, args.stop, conf, args.target, args.open_positions)
    if args.json:
        print(json.dumps(out, indent=2))
        return 0 if out["verdict"] == "PASS" else 3

    print(f"{out['verdict']}\n")
    if out["verdict"] == "BLOCK":
        for reason in out["reasons"]:
            print(f"   - {reason}")
        print("\nNothing sized. Change the stop, the capital or the limit and ask again.")
        return 3

    print(f"   shares                {out['shares']:,}")
    print(f"   position              {out['position_value']:,.2f}  "
          f"({out['position_percent_of_capital']}% of capital)")
    print(f"   stop                  {out['stop']:,.2f}  ({out['stop_percent']}% away)")
    print(f"   risk budget           {out['risk_budget']:,.2f}")
    print(f"   costs, round trip     {out['costs_round_trip']:,.2f}")
    print(f"   loss if stopped       {out['loss_if_stopped']:,.2f}")
    if "target" in out:
        print(f"   gain if target        {out['gain_if_target']:,.2f}")
        print(f"   reward to risk        {out['reward_to_risk']}")
    if conf.get("_from"):
        print(f"\n   capital and limits read from {conf['_from']}")
    for note in out["notes"]:
        print(f"\n   note: {note}")
    print("\nSizing is arithmetic, not a recommendation. This tool cannot place an order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
