#!/usr/bin/env python3
"""Scan the whole market for setups firing today, and show each rule's record beside them.

    python scan.py universe                        # the NSE equity list -> nse/universe.txt
    python scan.py --all                           # the cached daily scan, every rule's record
    python scan.py daily --live                    # the US pack's daily scan on daily closes
    python scan.py orb                             # after 09:35 IST: opening-range breakouts
    python scan.py orb --at 10:15                  # cut today's clock at 10:15 IST
    python scan.py orb --offline DIR --at "2026-10-22 10:15"   # replay saved answers

A screener that lists candidates without the rule's history invites you to act on a rule nobody
checked. So every result here carries what nse-backtest measured for that rule: the trade count,
the average return, and the edge over simply buying anything. Where a rule has a negative edge the
candidates are still listed and the header says plainly that the rule lost money.

A scan is a list of charts that match a rule, not a list of stocks to buy. Exit code 0 when the
scan found something, 1 when it did not (or the market is shut), 2 when the input was refused.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from nsetools import show                                        # noqa: E402
from nsetools.store import Prices                                 # noqa: E402

# The setups come from nsetools.setups, the same module /nse-backtest measures them from, so a rule
# can never be screened on one definition and measured on another. This used to load them out of
# the nse-backtest FOLDER by path, which broke the moment this skill was installed on its own.
from nsetools.setups import CLOSE_ONLY, MIN_TURNOVER_LACS, SETUPS, rsi   # noqa: E402,F401

# How many trades before a win rate means anything. See record_line.
MEANINGFUL_TRADES = 100

# The US pack's defaults, with the price floor in rupees: 5 dollars is a penny-stock line, and 5
# rupees is about six cents, so the floor here is 20 rupees.
MIN_PRICE = 20.0
DAILY_MIN_VOLUME = 1_000_000
MAX_CONFIRM = 200          # stage-two full charts per orb scan, most traded first
PAUSE = 0.3                # seconds between requests, as the US pack waits
DEFAULT_UNIVERSE = "nse/universe.txt"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

# The US pack's words for the close-only rules, printed beside each hit.
LABEL = {
    "breakout": "20-day closing-high breakout",
    "bull-flag": "bull flag",
    "reclaim-ma20": "reclaimed the 20-day average",
    "oversold": "oversold (RSI under 30)",
    "oversold-bounce": "oversold bounce",
}

BARE_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


class Refused(Exception):
    """A plain one-line refusal: printed as it is, never a traceback."""


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def universe(limit: int | None = None) -> list[str]:
    """Every tradeable NSE company symbol, as nse/universe.txt holds them.

    Companies only. The daily bhavcopy carries about 2,706 symbols where the equity master list
    carries 2,359, and the difference is almost entirely exchange traded funds -- a fund is held
    rather than traded, so it sits near 100% delivery permanently and would top a delivery screen
    every single day without ever being a trade.
    """
    return [symbol for symbol, _name in universe_rows(limit)]


def universe_rows(limit: int | None = None, offline: str | None = None) -> list[tuple[str, str]]:
    """(SYMBOL, company name) for every company in the exchange's equity master list, EQ series.

    `offline` is a saved copy of the list (equities.json or .json.gz, or a folder that holds one,
    directly or in exchange/), so the list can be rebuilt with no network, as the US pack's
    universe --offline does.
    """
    if offline:
        import gzip
        where = Path(offline)
        found = ([where] if where.is_file() else
                 [where / n for n in ("equities.json.gz", "equities.json",
                                      "exchange/equities.json.gz", "exchange/equities.json")])
        path = next((p for p in found if p.exists()), None)
        if path is None:
            raise Refused(f"no saved equity list (equities.json or .json.gz) at {where}")
        try:
            raw = (gzip.decompress(path.read_bytes()) if path.name.endswith(".gz")
                   else path.read_bytes())
            body = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, EOFError) as err:
            raise Refused(f"{path} is not a readable saved equity list: {err}") from None
        body = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(body, list):
            raise Refused(f"{path} is not a saved equity list (a list of rows)")
        rows = [r for r in body if isinstance(r, dict)
                and str(r.get("series") or "EQ").strip().upper() == "EQ"]
    else:
        from nsetools import nse
        rows = nse.equities()
    named: dict[str, str] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            named.setdefault(symbol, " ".join(str(row.get("name") or
                                                  row.get("name_of_company") or "").split()))
    out = sorted(named.items())
    return out[:limit] if limit else out


def complete(rows: list[dict], at, step: int = 5) -> bool:
    """Every bar from the 09:15 open to the scan time is there, one per slot.

    A missing bar is a stretch in which nothing traded: a halt, a stock locked at its upper or
    lower circuit, or one too thin to trade every five minutes. The US pack refuses such a chart
    for the same reason, and in India it matters more, because a circuit-locked stock prints no
    trades and its last close above a range is a price nobody could buy at.
    """
    from datetime import timedelta

    from nsetools import clock

    if not rows:
        return False
    opened = rows[0]["start"].replace(hour=clock.OPEN.hour, minute=clock.OPEN.minute, second=0,
                                      microsecond=0)
    close = opened.replace(hour=clock.CLOSE.hour, minute=clock.CLOSE.minute)
    cutoff = min(at, close)
    want = int((cutoff - opened).total_seconds() // (step * 60))
    done = [b for b in rows if b["end"] <= cutoff]
    if want < 1 or len(done) != want:
        return False
    return all(b["start"] == opened + timedelta(minutes=step * k) for k, b in enumerate(done))


def shortlist(symbols: list[str], at, or_minutes: int = 15, interval: str = "5m",
              min_price: float = MIN_PRICE, until=None, offline: str | None = None,
              pause: float = 0.5, report: dict | None = None, progress=None) -> list[dict]:
    """STAGE ONE: which symbols might have broken out, from closes alone, twenty a request.

    The batch endpoint returns closes and nothing else, so it cannot apply the real rule: the
    opening-range high is the highest HIGH of the range bars, and a close says nothing about a high.
    What it CAN do is rule symbols out, safely.

    WHY THIS LOSES NOTHING. A bar's close is never above its own high. So the highest CLOSE of the
    range bars is at or below the range's true high. Any bar that really closed above the true high
    therefore also closed above the highest close, and passes this shortlist. The test is the US
    pack's own, a close AT OR above the highest range close, so the candidates and their count are
    the reference's (a close level with it cannot break by itself, but it is counted). Every real
    breakout
    survives stage one; stage two then removes the ones that only cleared the lower bar. The tests
    check that property on real sessions rather than trusting the argument.

    THE RANGE IS 09:15 PLUS or_minutes, fixed by the clock, never by the first bar the feed sent.
    Anchored on the first bar, a batch answer missing its 09:15 close shifted the whole range five
    minutes late, and INFY's real 09:30 break of 2026-10-16 fell inside it and was lost. With a
    range bar missing, the highest close of the range bars that ARE there is still at or below the
    true range high, so the test stays safe; the symbol is kept and marked `range_partial`. With
    no range bar at all, or no bar after the range at all, there is nothing to compare, so the
    symbol is passed on to stage two rather than dropped unseen.

    `report`, when given, receives the counts: asked, fetched (came back with bars for the day),
    failed {symbol: why}, under_price, partial_range.
    """
    from datetime import datetime, timedelta

    from nsetools import bars, clock

    failed: dict[str, str] = {}
    found = bars.spark(symbols, interval, at=at, offline=offline, pause=pause, failed=failed,
                       progress=progress)
    step = bars.MINUTES[interval]
    opened = datetime.combine(at.date(), clock.OPEN, tzinfo=at.tzinfo)
    ends = opened + timedelta(minutes=or_minutes)
    slots = [opened + timedelta(minutes=step * k) for k in range(or_minutes // step)]
    out, under_price, partial = [], 0, 0
    for symbol, rows in found.items():
        # NOT checked for completeness here, unlike stage two. Measured on 2026-10-22: the batch
        # feed dropped ITC's 13:10 and 13:15 bars, in which 99,035 and 70,544 shares traded, so a
        # gap in this feed is the feed's gap, not a halt. Refusing on it lost a real breakout.
        # Stage two judges completeness on the full chart, which is the record.
        if not rows:
            continue
        inside = [b for b in rows if b["start"] < ends]
        missing = [s.strftime("%H:%M") for s in slots if s not in {b["start"] for b in inside}]
        later = [b for b in rows if b["start"] >= ends]
        after = [b for b in later if until is None or b["end"].time() <= until]
        if later and not after:
            continue                                             # every later bar is past `until`
        if rows[-1]["close"] < min_price:
            under_price += 1
            continue
        if missing:
            partial += 1
        if not inside or not later:
            # No range close, or no close after the range: the batch feed left a gap where the
            # clock says bars were due. No safe way to rule it out, so stage two decides.
            hit = {"symbol": symbol, "close_high": None, "first_close_above": None, "lead": None}
            if missing:
                hit["range_partial"] = missing
            out.append(hit)
            continue
        bar_high = max(b["close"] for b in inside)
        broke = next((b for b in after if b["close"] >= bar_high), None)
        if broke:
            hit = {"symbol": symbol, "close_high": bar_high, "first_close_above": broke["close"],
                   "lead": (broke["close"] - bar_high) / bar_high}
            if missing:
                hit["range_partial"] = missing
            out.append(hit)
    out.sort(key=lambda h: (h["lead"] is None, -(h["lead"] or 0)))
    if report is not None:
        report.update({"asked": len(symbols), "fetched": len(found), "failed": failed,
                       "under_price": under_price, "partial_range": partial})
    return out


MIN_DAILY_BARS = 30


def saved_batches(folder: str | Path) -> list:
    """The saved batch answers in a folder (or file), or a plain refusal saying why not."""
    from nsetools import bars
    try:
        return bars._saved_answers(str(folder))
    except bars.NoBars as err:
        raise Refused(str(err)) from None


def offline_symbols(folder: str | Path) -> list[str]:
    """Every symbol in a folder (or file) of saved batch answers, so a replay scans what it holds."""
    from nsetools import bars
    names = []
    for body in saved_batches(folder):
        for result in bars._results(body):
            names.append(str(result.get("symbol") or "").upper().removesuffix(".NS"))
    return sorted(_unique(names))


def offline_last_day(folder: str | Path):
    """The latest session in saved batch answers: the day a bare --at HH:MM replays, as the US
    pack replays the last day in its file."""
    from datetime import datetime

    from nsetools import bars
    from nsetools.clock import IST
    days = set()
    for body in saved_batches(folder):
        for result in bars._results(body):
            stamps, _closes = bars._stamps_closes(bars._series(result))
            days.update(datetime.fromtimestamp(s, IST).date() for s in stamps if bars._price(s))
    return max(days) if days else None


def daily_live(symbols: list[str], at, min_price: float = MIN_PRICE,
               min_volume: int = DAILY_MIN_VOLUME, offline: str | None = None,
               pause: float = 0.5, progress=None) -> tuple[list[dict], dict, str]:
    """The pre-market scan with no cache at all: daily closes for the market, twenty a request.

    The US pack's daily scan, the same rules on the same closes. It needs only closes, so it runs
    the five rules that use nothing else; the delivery rules need the exchange's own delivery
    figure, which only the cached bhavcopy carries, and stay with the cached scan.

    Liquidity from the batch itself: the day's price and its volume. The price floor is 20 rupees
    rather than the US pack's 5 dollars -- that is a penny-stock line in dollars, and 5 rupees is
    about six cents. The volume floor is in shares, so it carries over as it is.
    """
    from nsetools import bars

    failed: dict[str, str] = {}
    data = bars.spark_daily(symbols, at=at, offline=offline, pause=pause, failed=failed,
                            progress=progress)
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0,
              "failed_symbols": failed}
    hits, dates = [], {}
    for symbol in symbols:
        got = data.get(symbol)
        if not got:
            continue
        counts["fetched"] += 1
        closes = got["closes"]
        price = got.get("price") if got.get("price") is not None else (
            closes[-1][1] if closes else None)
        volume = got.get("volume")
        if price is None or price < min_price or (min_volume > 0 and
                                                  (volume is None or volume < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(closes) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        last_day = closes[-1][0]
        dates[last_day] = dates.get(last_day, 0) + 1
        series = []
        for k, (day, close) in enumerate(closes):
            series.append({"traded_on": day, "close": close,
                           "previous_close": closes[k - 1][1] if k else None})
        i = len(series) - 1
        tags = [name for name in CLOSE_ONLY if SETUPS[name](series, i, 20)]
        if tags:
            value = rsi([c for _, c in closes])
            hits.append({"symbol": symbol, "setups": tags, "price": round(closes[-1][1], 2),
                         "close_date": last_day,
                         "rsi": round(value, 1) if value is not None else None,
                         "volume": int(volume) if volume is not None else None})
    # Most rupees traded first: price times volume.
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))
    last = max(dates, key=lambda d: (dates[d], d)) if dates else ""
    counts["hits"] = len(hits)
    return hits, counts, last


def liquidity(at, offline: str | None = None) -> tuple[dict[str, float], str]:
    """Each symbol's turnover in lakhs on the PREVIOUS trading day, and which day that was.

    Stage two can only afford so many full-bar checks, so it takes the most traded first, the way
    the US pack does. Ranking by how far a close jumped instead hands every slot to tiny illiquid
    names, which measured live on 2026-10-23 filled the top of the list with stocks whose
    "breakout" bar had no trades in it at all.

    The PREVIOUS day, never today's: today's closing turnover is not known at 11:00, and ranking on
    it would be the scan seeing the future. One request covers the whole market. A saved file that
    cannot be read is the same as no file: no ranking, and the scan says so.

    Only KNOWN turnovers are returned. A row with no turnover figure, or a symbol the file does not
    carry, is left out rather than set to zero: a zero would drop it as "too thinly traded", which
    is a figure the exchange never gave.
    """
    from nsetools import clock, nse

    if offline:
        path = Path(offline) / "exchange" / "bhavcopy.json.gz"
        if not path.exists():
            return {}, ""
        import gzip
        try:
            body = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
            body = body.get("data", body)
            day = str(body.get("day", ""))
            rows = body["rows"]
        except (OSError, ValueError, EOFError, KeyError, TypeError, AttributeError):
            return {}, ""
    else:
        try:
            before = clock.previous_trading_day(at.date())
            day, rows = nse.last_trading_day(before)
        except Exception:                                        # noqa: BLE001
            return {}, ""
        day = str(day)
    # A file from the scanned day or later is the future for that scan: replaying 2026-10-22 on
    # 2026-10-23's turnover ranked the morning on figures nobody had yet.
    if not day or day >= at.date().isoformat():
        return {}, ""
    traded = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol, value = str(row.get("symbol") or "").strip().upper(), row.get("turnover")
        if symbol and isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(value) and value >= 0:
            traded[symbol] = float(value)
    return (traded, day) if traded else ({}, "")


def opening_range_scan(symbols: list[str], at, or_minutes: int = 15, interval: str = "5m",
                       min_price: float = MIN_PRICE, min_volume_x: float | None = None,
                       until=None, min_volume: int | None = None,
                       offline: str | None = None, failed: dict | None = None,
                       partial: dict | None = None, pause: float = 0.0,
                       report: dict | None = None, progress=None) -> list[dict]:
    """STAGE TWO: which of these really CLOSED a bar above their opening range, on full bars.

    The same question /nse-entry asks of one symbol, asked of a list. One request per symbol, which
    is why it is fed the shortlist from stage one rather than the whole market, and a polite
    `pause` between requests when they go to the network, as the US pack waits.

    `until` is the no-new-entries time: a break after it does not count. `min_volume` is the shares
    traded so far in the session, a floor against breaks on nothing.

    `failed`, when given, collects {symbol: why} for every chart that could not be read or had a
    missing bar, as the US pack counts them: such a symbol is neither a hit nor a miss, and
    dropping it without a word let a failed fetch read as "nothing broke". `partial` maps a symbol
    to the range bars stage one's batch feed was missing; a hit then carries them as
    `stage1_range_partial`. The hit's own range is always from the complete full chart.
    `report`, when given, receives `under_floor`: charts read and complete but under the price,
    volume or volume-ratio floor, which the US pack counts as illiquid; and `unusable`: charts
    read with a missing bar. `progress(n)`, when given, is told every fifty charts.
    """
    from nsetools import bars

    hits, under_floor, unusable = [], 0, 0
    for number, symbol in enumerate(symbols):
        if number and pause and not offline:
            time.sleep(pause)
        if progress is not None and number and number % 50 == 0:
            progress(number)
        try:
            rows = bars.candles(symbol, interval, at=at, offline=offline)
        except Exception as err:                                 # noqa: BLE001  no bars, not a hit
            if failed is not None:
                failed[symbol] = str(err) or type(err).__name__
            continue
        if not complete(rows, at):
            unusable += 1
            if failed is not None:
                failed[symbol] = ("the full chart has a missing bar (a halt, a circuit, or no "
                                  "trades)")
            continue                                             # a missing bar: a halt or no trade
        window = bars.opening_range(rows, or_minutes)
        if not window:
            continue
        after = [b for b in rows if b["start"] >= window["to"]]
        if not after:
            continue
        last = after[-1]
        if last["close"] < min_price:
            under_floor += 1
            continue
        # Known volume only. The session total is then a FLOOR on the true figure, since the feed
        # never reports the 09:15 bar; a stock that clears min_volume on known volume alone
        # certainly clears it in fact, and that is the safe direction for the check to err.
        if min_volume and sum(bars.known_volume(rows)) < min_volume:
            under_floor += 1
            continue
        # Nobody traded in a flat bar on no volume; the feed carried the last price forward, so a
        # close above the range there is a price nobody paid. A bar that moved but reports no
        # volume did trade, and is not refused.
        broke = next((b for b in after if b["close"] > window["high"] and bars.traded(b)
                      and (until is None or b["end"].time() <= until)), None)
        if not broke:
            continue
        ratio = (broke["volume"] / window["volume"]
                 if broke["volume"] is not None and window["volume"] else None)
        if min_volume_x and (ratio is None or ratio < min_volume_x):
            under_floor += 1
            continue                                             # unknown volume cannot clear a floor
        above = round((broke["close"] / window["high"] - 1) * 100, 2)      # the US pack's sum
        hit = {
            "symbol": symbol,
            "setups": ["orb"],
            "triggered": broke["end"].strftime("%H:%M"),
            "bar_start": broke["start"].strftime("%H:%M"),
            # Counted FROM 1 (the 09:15 bar is bar 1), as nse-entry and nse-exit count it. The US
            # pack counts from 0; a reader taking this file there must subtract one.
            "bar_index": rows.index(broke) + 1,
            "entry": broke["close"],
            "range_high": window["high"],
            "range_low": window["low"],
            "above_percent": above,
            "volume_x": round(ratio, 2) if ratio is not None else None,
            "last": last["close"],
            # The same facts under the US pack's names, so a reader written for its watchlist
            # finds them: price (the breaking close), at (when that bar closed), or_high,
            # or_low and pct_over.
            "price": broke["close"],
            "at": broke["end"].strftime("%H:%M"),
            "or_high": window["high"],
            "or_low": window["low"],
            "pct_over": above,
        }
        if partial and partial.get(symbol):
            hit["stage1_range_partial"] = partial[symbol]
        hits.append(hit)
    # Earliest break first, as the US pack orders them: the first to break is the one that has
    # held longest, and a list ordered by size puts the furthest-run (and latest-to-enter) first.
    hits.sort(key=lambda h: (h["triggered"], h["symbol"]))
    if report is not None:
        report["under_floor"] = under_floor
        report["unusable"] = unusable
    return hits


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


def symbols_from_file(path: str | Path) -> list[str]:
    """Symbols from a watchlist.json (its hits), a JSON list, or a text file, as the US pack reads.

    Text: one symbol a line, `SYMBOL|Name` allowed, blank lines and `#` comments skipped. A missing
    file or broken JSON is one plain line, not a traceback.
    """
    path = Path(path)
    if not path.is_file():
        raise Refused(f"no file at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise Refused(f"{path} cannot be read as text: {err}") from None
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as err:
            raise Refused(f"{path} is not valid JSON: {err}") from None
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        if not isinstance(items, list):
            raise Refused(f"{path} holds no list of symbols")
        return [str(x.get("symbol") if isinstance(x, dict) else x).strip().upper()
                for x in items or [] if x and (not isinstance(x, dict) or x.get("symbol"))]
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.split("|")[0].strip().upper())
    return out


def _unique(names: list[str]) -> list[str]:
    return list(dict.fromkeys(n for n in names if n))


def symbols_for(args, offline_folder: str | None = None) -> list[str]:
    """The list to scan, the US pack's way: named, a watchlist, every chart in the saved files
    (offline, unless --universe is given), or the universe file -- which must exist.

    Deduplicated in order, then cut to --limit. An empty list is refused: a scan of nothing is not
    a scan that found nothing.
    """
    if args.symbols:
        names = [s.strip().upper() for s in re.split(r"[,\s]+", args.symbols) if s.strip()]
    elif args.watchlist:
        names = symbols_from_file(args.watchlist)
    elif offline_folder and not args.universe:
        names = offline_symbols(offline_folder)
        if not names:
            raise Refused(f"no symbols in the saved answers in {offline_folder}")
    else:
        path = Path(args.universe or DEFAULT_UNIVERSE)
        if not path.exists():
            raise Refused(f"no {path} yet: run `scan.py universe` first, or pass --symbols")
        names = symbols_from_file(path)
    names = _unique(names)
    if args.limit:
        names = names[:args.limit]
    if not names:
        raise Refused("no symbols to scan")
    return names


def _refuse(args, message: str) -> None:
    """A refusal or a closed market, in one line. With --json it goes to stderr, as the US pack
    sends every refusal there, so what --json prints is always the JSON document or nothing."""
    print(message, file=sys.stderr if getattr(args, "json", False) else sys.stdout)


def progress(args, message: str) -> None:
    """A progress line, to stderr as the US pack prints it, and not at all with --json/--quiet."""
    if not (args.json or args.quiet):
        print(message, file=sys.stderr)


def _every_fifty(args, what: str):
    """A live fetch's running count, every fifty requests once there are more than 25 of them,
    as the US pack reports a whole-market fetch."""
    def tell(done: int, total: int) -> None:
        if total > 25 and done % 50 == 0:
            progress(args, f"  {what} {done:,} of {total:,} requests")
    return tell


def _n(value: float) -> str:
    """A floor as the US pack prints one: 20, or 20.50. Never 1e+06."""
    value = float(value)
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def check_offline(args, inner: str) -> str | None:
    """The saved-answers folder for this mode, or None live. A missing folder is refused plainly."""
    if not args.offline:
        return None
    home = Path(args.offline)
    if not home.is_dir():
        raise Refused(f"no offline folder at {home} (it wants {inner}/ inside)")
    return str(home / inner)


def daily(args, at) -> int:
    """`daily --live`: the US pack's pre-market scan, on the market's own daily closes."""
    from datetime import timezone

    started = time.time()
    spark_dir = check_offline(args, "spark-daily")
    names = symbols_for(args, spark_dir)
    floor_price = args.min_price if args.min_price is not None else MIN_PRICE
    floor_volume = args.min_volume if args.min_volume is not None else DAILY_MIN_VOLUME

    if not spark_dir:
        progress(args, f"Fetching daily closes for {len(names):,} symbols "
                       f"({-(-len(names) // 20)} batch requests of up to 20)...")
    from nsetools import bars
    try:
        hits, counts, last = daily_live(names, at, floor_price, floor_volume, spark_dir,
                                        args.pause, progress=_every_fifty(args, "batch"))
    except bars.NoBars as err:
        raise Refused(str(err)) from None
    failed = len(names) - counts["fetched"]
    doc = {"created_by": "nse-scan",
           # The scan's own clock, so a replay says when it was replayed AS, like the US pack.
           "created_at": at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
           "mode": "daily", "as_of": f"{last} close" if last else None, "universe": len(names),
           "scanned": counts["scanned"], "failed": failed, "source": "batch daily closes",
           "filters": {"min_price": floor_price, "min_volume": floor_volume},
           "note": NOTE, "failed_symbols": counts["failed_symbols"], "hits": hits}
    if args.offline:
        doc["offline"] = Path(args.offline).name
    where = None
    if not args.no_write:
        where = show.hand_off("watchlist.json", doc, args.out)
    if args.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - started, 1)),
                         indent=2, default=str))
        return 0 if hits else 1
    print(f"Daily scan, {at:%a %Y-%m-%d %H:%M} IST, closes through {last or 'nothing'} "
          f"(batch daily closes only).")
    print(f"Fetched {counts['fetched']:,} of {len(names):,} charts"
          + (f" ({failed:,} failed or had no data)" if failed else "")
          + f". {counts['fetched'] - counts['illiquid']:,} passed the liquidity filter "
            f"(Rs {_n(floor_price)}+ and {floor_volume:,}+ shares)"
          + (f", {counts['short']} had under {MIN_DAILY_BARS} days" if counts["short"] else "")
          + f"; {counts['scanned']:,} scanned.")
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k in CLOSE_ONLY]
        print(f"{_plural(len(hits), 'chart')} matched a setup: "
              + ", ".join(f"{n} {LABEL[k]}" for k, n in per if n) + ".")
        print("Most rupees traded first (price x volume):")
    elif counts["fetched"] == 0:
        print(f"None of the {len(names):,} charts could be read, so nothing can be said about "
              f"setups: this is a failed scan, not an empty one.")
    elif failed:
        print(f"No setups among the {counts['fetched']:,} charts that came back; {failed:,} could "
              f"not be read, so this is not the whole list.")
    else:
        print("No setups.")
    for hit in hits[:args.show]:
        rsi_text = f"RSI {hit['rsi']:5.1f}" if hit["rsi"] is not None else "RSI   n/a"
        print(f"  {hit['symbol']:<12} Rs {hit['price']:>10,.2f}  {rsi_text}  "
              f"{', '.join(LABEL.get(t, t) for t in hit['setups'])}")
    if len(hits) > args.show:
        print(f"  ... and {len(hits) - args.show} more in the watchlist (--show N for more).")
    if failed:
        names_failed = sorted(counts["failed_symbols"])
        print(f"COULD NOT READ: {', '.join(names_failed[:12])}"
              + (" ..." if len(names_failed) > 12 else "") + " (reasons in the file).")
    if where:
        print(f"Wrote {where} (mode daily, {_plural(len(hits), 'hit')}).")
    print(NOTE + " Next: pick one chart, then /nse-news SYMBOL.")
    return 0 if hits else 1


def _until(args) -> "object":
    """no_entries_after: --until, else the config's, else 15:00. A bad one is refused plainly."""
    from datetime import time as clock_time

    from nsetools import settings

    try:
        conf = settings.load(args.config)
    except FileNotFoundError as err:
        raise Refused(f"{err}: name a config that exists, or leave --config out") from None
    raw = args.until or conf.get("no_entries_after") or "15:00"
    source = "--until" if args.until else ("no_entries_after in " + conf["_from"]
                                           if conf.get("no_entries_after") else "--until")
    match = BARE_TIME.match(str(raw))
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise Refused(f"{source} wants a time like 15:00 (IST), got {raw!r}")
    return clock_time(int(match.group(1)), int(match.group(2)))


def orb(args, at, pinned_now) -> int:
    """Today's opening-range breaks, in two stages, so a whole-market scan is affordable."""
    from datetime import datetime as _dt, timedelta as _td, timezone

    from nsetools import bars, clock

    started = time.time()
    # The file's own date is the clock's -- --now, or the real time -- never the --at cutoff, as
    # the US pack stamps it: a 10:15 scan run at 11:00 was created at 11:00.
    created = (pinned_now or clock.now()).astimezone(timezone.utc).replace(microsecond=0)
    # The cutoff is on the minute, as the US pack cuts it: a run at 15:00:40 is a 15:00 scan, not
    # one refused as later than a 15:00 no_entries_after.
    at = at.replace(second=0, microsecond=0)
    until = _until(args)
    spark_dir = check_offline(args, "spark")
    bars_dir = str(Path(args.offline) / "bars") if args.offline else None

    # The US pack's order: the range length, the calendar, then the clock.
    if args.or_minutes < 5 or args.or_minutes % 5 or args.or_minutes > 120:
        _refuse(args, "--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic).")
        return 2
    try:
        trading = clock.is_trading_day(at.date(), offline=bool(args.offline))
    except clock.Unknown as err:
        _refuse(args, f"{err}. Replay a day from a year whose holiday list is known.")
        return 1
    if not trading:
        # The US pack's line: why there is no session, and what to do instead.
        _refuse(args, clock.status(at, offline=bool(args.offline)) + " Run orb on a trading day "
                      "after the opening range, or replay a past session with --offline.")
        return 1

    if not args.offline:
        # Live bars with a clock in the future would take the bar still forming as closed, and
        # the free feed keeps only five days of five-minute bars. The US pack refuses both.
        real = clock.now()
        limit = min(real, pinned_now) if pinned_now else real
        if at > limit + _td(seconds=59):
            _refuse(args, f"--at {at:%Y-%m-%d %H:%M} is later than now "
                          f"({limit:%Y-%m-%d %H:%M} IST).")
            return 2
    elif pinned_now and at > pinned_now + _td(seconds=59):
        _refuse(args, f"--at {at:%H:%M} is later than --now ({pinned_now:%Y-%m-%d %H:%M} IST).")
        return 2

    range_end = _dt.combine(at.date(), clock.OPEN, tzinfo=at.tzinfo) + _td(minutes=args.or_minutes)
    first_close = range_end + _td(minutes=5)
    close_dt = _dt.combine(at.date(), clock.CLOSE, tzinfo=at.tzinfo)
    if at > close_dt and args.at:
        _refuse(args, f"--at {at:%H:%M} is after the 15:30 close on {at:%a %Y-%m-%d}.")
        return 2
    if not args.at and at >= close_dt:
        _refuse(args, f"The market closed at 15:30 IST on {at:%a %Y-%m-%d}. Replay the session "
                      f"with --at \"{at:%Y-%m-%d} 10:15\".")
        return 1
    if at < first_close:
        _refuse(args, f"It is {at:%H:%M} IST: the {args.or_minutes}-minute opening range runs "
                      f"09:15 to {range_end:%H:%M} and the first bar after it closes at "
                      f"{first_close:%H:%M}. Run orb again after {first_close:%H:%M}.")
        return 1
    if at.time() > until:
        _refuse(args, f"It is {at:%H:%M} IST, after {until:%H:%M}, your last time for new "
                      f"entries (no_entries_after): a breakout found now is expired. Run orb "
                      f"tomorrow after {first_close:%H:%M}, or replay today with --at "
                      f"\"{at:%Y-%m-%d} 10:15\".")
        return 1
    if not args.offline and (clock.now().date() - at.date()).days > 6:
        _refuse(args, f"The free five-minute history is short: replay {at:%Y-%m-%d} with "
                      f"--offline and saved bars.")
        return 2

    names = symbols_for(args, spark_dir)
    min_price = args.min_price if args.min_price is not None else MIN_PRICE

    say = (lambda *_a, **_k: None) if (args.quiet or args.json) else print
    if not args.json:
        print(f"Opening-range breakout scan, {at:%a %Y-%m-%d %H:%M} IST: {args.or_minutes}-minute "
              f"range (09:15 to {range_end:%H:%M}), 5-minute bars, no new entries after "
              f"{until:%H:%M}.")
    if not spark_dir:
        progress(args, f"Stage 1: 5-minute closes for {len(names):,} symbol(s) "
                       f"({-(-len(names) // 20)} batch request(s) of up to 20)...")

    stage1: dict = {}
    try:
        short = shortlist(names, at, args.or_minutes, "5m", min_price, until, spark_dir,
                          args.pause, report=stage1, progress=_every_fifty(args, "batch"))
    except bars.NoBars as err:
        raise Refused(str(err)) from None
    failed = dict(stage1.get("failed") or {})
    say(f"         {stage1['fetched']:,} of {len(names):,} came back"
        + (f" ({len(failed):,} could not be read)" if failed else "")
        + f"; {len(short)} closed at or above their opening range's highest close (or could "
          f"not be ruled out).")

    traded, as_of = liquidity(at, args.offline)
    illiquid, unknown = [], []
    if traded:
        # Too thin only on a KNOWN turnover under the floor. A symbol the file has no figure for
        # (listed since, say) is not called thin on a number nobody published: it is kept, and
        # ranked after every known one.
        illiquid = [h["symbol"] for h in short
                    if h["symbol"] in traded and traded[h["symbol"]] < MIN_TURNOVER_LACS]
        unknown = [h["symbol"] for h in short if h["symbol"] not in traded]
        short = [h for h in short if h["symbol"] not in illiquid]
        short.sort(key=lambda h: (h["symbol"] not in traded, -traded.get(h["symbol"], 0)))
        say(f"         {len(illiquid)} dropped as too thinly traded (under "
            f"{MIN_TURNOVER_LACS:g} lakh on {as_of}); the rest ranked most traded first"
            + (f" ({len(unknown)} with no turnover figure on {as_of}, kept and ranked last)"
               if unknown else "") + ".")
    else:
        say(f"         (no turnover from a trading day before {at:%Y-%m-%d} available, so not "
            f"filtered for liquidity and ranked by how far each close led)")

    confirm = [h["symbol"] for h in short]
    left_out = []
    if args.max_confirm and len(confirm) > args.max_confirm:
        left_out = confirm[args.max_confirm:]
        confirm = confirm[:args.max_confirm]
    say(f"Stage 2: full bars for {len(confirm)}, to apply the real rule on the bar HIGHS.")

    failed2: dict[str, str] = {}
    stage2: dict = {}
    partial = {h["symbol"]: h["range_partial"] for h in short if h.get("range_partial")}
    hits = opening_range_scan(confirm, at, args.or_minutes, "5m", min_price,
                              args.min_volume_x, until, args.min_volume, bars_dir,
                              failed=failed2, partial=partial, pause=args.pause, report=stage2,
                              progress=None if args.offline else (
                                  lambda n: progress(args, f"  stage 2: {n:,} full charts so far")))
    checked = len(confirm) - len(failed2)
    failed.update(failed2)
    say(f"         {checked} checked" + (f", {len(failed2)} could not be read or had a missing bar"
                                         if failed2 else "")
        + f"; {len(hits)} really did close above the range high.\n")

    # Under the floor: the price floor on every chart in stage one, and the volume floors on the
    # charts stage two read. `scanned` is what is left, as the US pack counts it: fetched less
    # what fell under a floor, so the two add up rather than overlap.
    under_floor = stage1.get("under_price", 0) + stage2.get("under_floor", 0)
    scanned = stage1["fetched"] - under_floor
    shortlisted = len(short) + len(illiquid)
    counts = {"asked": len(names), "requested": len(names), "fetched": stage1["fetched"],
              "stage1_failed": len(stage1.get("failed") or {}),
              "under_price": stage1.get("under_price", 0), "illiquid": under_floor,
              "unusable": stage2.get("unusable", 0),
              "partial_range": stage1.get("partial_range", 0), "scanned": scanned,
              "shortlisted": shortlisted, "candidates": shortlisted, "too_thin": len(illiquid),
              "no_turnover_figure": len(unknown),
              "sent_to_stage2": len(confirm), "confirmed": checked,
              "stage2_failed": len(failed2), "not_checked": len(left_out),
              "unconfirmed": len(left_out), "hits": len(hits)}
    doc = {"created_by": "nse-scan",
           "created_at": created.isoformat(),
           "mode": "orb", "as_of": at.isoformat(), "universe": len(names),
           "asked": len(names), "scanned": scanned, "failed": len(failed),
           "failed_symbols": failed, "unconfirmed": len(left_out),
           "source": "batch closes (stage 1), full five-minute charts (stage 2)",
           "or_minutes": args.or_minutes, "interval": "5m", "until": until.strftime("%H:%M"),
           "filters": {"min_price": min_price, "min_volume": args.min_volume,
                       "min_volume_x": args.min_volume_x,
                       "min_turnover_lacs": MIN_TURNOVER_LACS if traded else None},
           "note": NOTE,
           "shortlisted": shortlisted, "too_thin": len(illiquid),
           "liquidity_from": as_of, "confirmed": checked, "not_checked": left_out,
           "hits": hits}
    if args.offline:
        doc["offline"] = Path(args.offline).name

    where = None
    if not args.no_write:
        # Written with no hits too. Writing only on a hit left the last run's hits in place, and
        # nse-entry then re-checked a list this scan had just found empty.
        where = show.hand_off("watchlist.json", doc, args.out)
    if args.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - started, 1)),
                         indent=2, default=str))
        return 0 if hits else 1

    # The reference's summary: what was scanned, what came back, what was usable, what was checked.
    print(f"Fetched {stage1['fetched']:,} of {len(names):,} charts"
          + (f" ({counts['stage1_failed']:,} failed or had no data)" if counts["stage1_failed"]
             else "")
          + f"; {scanned:,} passed the price / volume floor (Rs {_n(min_price)}"
          + (f", {args.min_volume:,} shares" if args.min_volume else "")
          + (f", {_n(args.min_volume_x)}x volume" if args.min_volume_x else "") + ")"
          + (f" ({under_floor:,} under it)" if under_floor else "")
          + f"; {_plural(shortlisted, 'candidate')}, {checked:,} confirmed with real highs and "
            f"lows"
          + (f" ({len(failed2):,} could not be read or had a missing bar)" if failed2 else "")
          + ".")
    if left_out:
        # Said out loud, never dropped quietly. These passed stage one and were not checked, so
        # they are neither hits nor misses.
        ranked = "most traded" if traded else "furthest-leading"
        print(f"PARTIAL: {len(left_out):,} more candidates were not checked (only the "
              f"{args.max_confirm:,} {ranked} are; --max-confirm 0 checks all): "
              f"{', '.join(left_out[:12])}" + (" ..." if len(left_out) > 12 else "") + ".")
    whole = not (left_out or failed2 or illiquid)
    if hits:
        print(f"{len(hits)} of " + (f"{scanned:,}" if whole else f"the {checked:,} checked")
              + f" charts closed above their opening-range high by {at:%H:%M}:")
        show.table(hits[:args.show],
                   [("triggered", "broke at", 8), ("symbol", "symbol", 12),
                    ("entry", "entry", 10), ("range_low", "range low", 10),
                    ("range_high", "range high", 10), ("above_percent", "above%", 7),
                    ("volume_x", "vol x", 6)])
        if len(hits) > args.show:
            print(f"  ... and {len(hits) - args.show} more in the watchlist (--show N for more).")
        flagged = [h["symbol"] for h in hits if h.get("stage1_range_partial")]
        if flagged:
            print(f"Stage one's batch feed was missing a range bar for {', '.join(flagged)}; the "
                  f"range above is from the complete full chart.")
    elif stage1["fetched"] == 0:
        print(f"None of the {len(names):,} charts could be read, so nothing can be said about "
              f"breakouts: this is a failed scan, not an empty one.")
    elif failed or left_out or illiquid:
        # A candidate dropped as too thin was never checked either, so it is named here too:
        # "no chart closed above" would include charts nobody looked at.
        print(f"No chart that was checked closed a bar above its opening-range high by "
              f"{at:%H:%M}; " + " and ".join(
                  part for part in (f"{len(failed):,} could not be read" if failed else "",
                                    f"{len(left_out):,} were not checked" if left_out else "",
                                    f"{len(illiquid):,} were dropped as too thinly traded"
                                    if illiquid else "")
                  if part) + ", so this is not the whole list.")
    else:
        print(f"No chart closed above its opening-range high by {at:%H:%M}.")

    if failed:
        names_failed = sorted(failed)
        print(f"COULD NOT READ: {', '.join(names_failed[:12])}"
              + (" ..." if len(names_failed) > 12 else "") + " (reasons in the file).")

    if where:
        print(f"Wrote {where} (mode orb, {_plural(len(hits), 'hit')}).")
    print(NOTE + " Next: pick one chart, then /nse-news SYMBOL and /nse-entry SYMBOL "
                 "(then /nse-exit sets the stop).")
    return 0 if hits else 1


def _count(text: str) -> int:
    """An argparse type: a whole number, zero or more."""
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"wants a whole number, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"wants zero or more, got {text!r}")
    return value


def _seconds(text: str) -> float:
    """An argparse type: a pause, zero or more seconds. A negative one crashed the first live
    request with a traceback (time.sleep refuses it)."""
    value = show.number(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"wants zero or more seconds, got {text!r}")
    return value


def _positive(text: str) -> int:
    value = _count(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"wants 1 or more, got {text!r}")
    return value


def resolve_clock(args):
    """(at, pinned_now): the moment scanned, and the --now clock if one was pinned.

    --now pins the clock to a date and a time in IST. --at is the scan's cutoff: a date and a time,
    or, for orb, a bare HH:MM on the clock's day -- today live, or with --offline and no --now the
    last session in the saved answers, as the US pack replays the last day in its file.
    """
    from datetime import datetime

    from nsetools import clock

    pinned = clock.now(args.now) if args.now else None
    if not args.at:
        return (pinned or clock.now()), pinned
    bare = BARE_TIME.match(args.at)
    if not bare:
        return clock.now(args.at), pinned
    if args.mode != "orb":
        raise Refused(f"--at wants a date and a time in IST for {args.mode}, like --at "
                      f"\"2026-10-22 {args.at.strip()}\"; a time alone is orb's cutoff")
    hour, minute = int(bare.group(1)), int(bare.group(2))
    if hour > 23 or minute > 59:
        raise Refused(f"--at wants an IST time like 10:15, not {args.at!r}")
    if pinned:
        day = pinned.date()
    elif args.offline:
        folder = Path(args.offline) / "spark"
        day = offline_last_day(folder) if folder.exists() else None
        if day is None:
            raise Refused(f"no saved session in {folder} to replay at {args.at.strip()}: give "
                          f"--at \"YYYY-MM-DD HH:MM\"")
    else:
        day = clock.now().date()
    return datetime.combine(day, datetime.min.time().replace(hour=hour, minute=minute),
                            tzinfo=clock.IST), pinned


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="daily", choices=("daily", "orb", "universe"),
                    help="daily: the end-of-day rules. orb: today's opening-range breaks. "
                         "universe: write the symbol list")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--symbols", help="tickers, comma or space separated, instead of the "
                                         "universe")
    group.add_argument("--watchlist", help="a watchlist.json (its hits), a JSON list, or a text "
                                           "file of symbols (one a line, SYMBOL|Name allowed)")
    ap.add_argument("--universe", nargs="?", const=DEFAULT_UNIVERSE, metavar="FILE",
                    help="symbol list, one SYMBOL|Name a line (default nse/universe.txt)")
    ap.add_argument("--limit", type=_count, help="scan only the first N symbols")
    ap.add_argument("--at", help="orb: cut the clock at HH:MM IST today, or at "
                                 "'2026-10-22 10:15'; daily: pin the clock")
    ap.add_argument("--now", help="pin the clock, like '2026-10-22 10:15' (IST)")
    ap.add_argument("--live", action="store_true",
                    help="daily: scan the market's daily closes now, with no cache needed")
    ap.add_argument("--until", help="no new entries after HH:MM (default: config "
                                    "no_entries_after, 15:00)")
    ap.add_argument("--min-price", type=show.number, default=None,
                    help="rupees, on the last close (20)")
    ap.add_argument("--min-volume", type=_count,
                    help="shares: daily --live the last day's (1,000,000), orb so far today (0)")
    ap.add_argument("--min-volume-x", type=show.number)
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--max-confirm", type=_count, default=MAX_CONFIRM,
                    help=f"full charts in stage two, most traded first ({MAX_CONFIRM}; 0 = all)")
    ap.add_argument("--pause", type=_seconds, default=PAUSE,
                    help=f"seconds between requests ({PAUSE})")
    ap.add_argument("--offline", help="saved responses: a folder with spark/ and bars/ (orb), "
                                      "spark-daily/ (daily) or equities.json.gz (universe)")
    ap.add_argument("--show", type=_count, default=50, help="hits to print; all are written")
    ap.add_argument("--quiet", action="store_true", help="no progress lines")
    ap.add_argument("--out", help="write the watchlist (or universe) here instead of nse/")
    ap.add_argument("--no-write", action="store_true", help="look without writing anything")
    ap.add_argument("--config", help="a config file instead of the usual places")
    ap.add_argument("--setup", choices=sorted(SETUPS), help="one rule")
    ap.add_argument("--all", action="store_true", help="every rule, worst record last")
    ap.add_argument("--window", type=_positive, default=20, help="the lookback the rule uses")
    ap.add_argument("--on", help="scan a past cached day instead of the latest, YYYY-MM-DD")
    ap.add_argument("--top", type=_count, default=20)
    ap.add_argument("--db", default="nse/prices.sqlite")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true", help="also write nse/scan.json")
    args = ap.parse_args(argv)

    from nsetools import settings
    try:
        if args.mode == "universe":
            return _universe(args)
        for flag, text in (("--now", args.now), ("--at", args.at)):
            if text and not BARE_TIME.match(text) and not _parses(text):
                _refuse(args, f"{flag} wants a time like \"2026-10-22 10:15\" (IST)"
                      + (", or HH:MM for orb's cutoff" if flag == "--at" else "")
                      + f", not {text!r}.")
                return 2
        at, pinned = resolve_clock(args)
        if args.mode == "orb":
            if args.offline and not (args.at or args.now):
                # Saved bars with the real clock would scan a day the files do not hold.
                _refuse(args, "offline replay: give --at HH:MM, --at \"YYYY-MM-DD HH:MM\" or --now "
                      "\"YYYY-MM-DD HH:MM\" (IST), the moment to replay")
                return 2
            return orb(args, at, pinned)
        if args.live or not Path(args.db).exists():
            if not args.live and not args.quiet and not args.json:
                print(f"No price cache at {args.db}, so this scans the market's daily closes "
                      f"directly instead (the delivery rules need the cache: nse-backtest "
                      f"--fetch).\n")
            return daily(args, at)
        return cached_daily(args, at if (args.at or args.now) else None)
    except (Refused, settings.BadConfig) as err:
        _refuse(args, f"scan.py: {err}")
        return 2
    except KeyboardInterrupt:
        # Ctrl-C mid-scan is one line, as the US pack says it, never a traceback. Nothing is
        # written: the watchlist is written whole or not at all.
        print("scan.py: stopped", file=sys.stderr)
        return 1


def _parses(text: str) -> bool:
    from nsetools import clock
    try:
        clock.now(text)
        return True
    except clock.BadTime:
        return False


def _universe(args) -> int:
    """`universe`: the exchange's equity master list, companies only, to nse/universe.txt."""
    started = time.time()
    try:
        rows = universe_rows(args.limit, args.offline)
    except Refused:
        raise
    except Exception as err:                                     # noqa: BLE001  said, not a trace
        _refuse(args, f"Could not fetch the NSE equity list ({err}). Nothing written; try again "
                      f"later.")
        return 1
    if not rows:
        _refuse(args, "The NSE equity list came back empty. Nothing written.")
        return 1
    path = Path(args.out) if args.out else show.shared("universe.txt")
    if not args.no_write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{s}|{n}\n" for s, n in rows), encoding="utf-8")
    source = (f"saved copy {Path(args.offline).name}" if args.offline
              else "nsearchives EQUITY_L.csv")
    if args.json:
        print(json.dumps({"count": len(rows), "source": source, "series": "EQ",
                          "out": None if args.no_write else str(path),
                          "seconds": round(time.time() - started, 1)}, indent=2))
        return 0
    print(f"NSE equity master list ({source}): {len(rows):,} companies in the EQ series"
          + (f" (the first {args.limit:,})" if args.limit else "") + ".")
    print(f"Kept {len(rows):,} NSE-listed companies; funds and ETFs are not in this list (the "
          f"daily bhavcopy carries about 2,706 symbols with them).")
    if not args.no_write:
        print(f"Wrote {path} (one SYMBOL|Name a line).")
    return 0


def _sessions_missing(last: str, now) -> str | None:
    """The newest session whose exchange file should be out by `now` and is after `last`, or None.

    Today's file is not counted until the evening it is published, so only a cache a whole session
    behind is called stale. A year whose holiday list is not known claims nothing either way.
    """
    from datetime import date

    from nsetools import clock
    try:
        due = clock.previous_trading_day(now.date(), offline=True)
    except clock.Unknown:
        return None
    return due.isoformat() if due > date.fromisoformat(last) else None


def cached_daily(args, when=None) -> int:
    """The default daily scan, on the price cache: every rule with its record beside it."""
    import sqlite3
    from datetime import datetime as _dt, timezone

    from nsetools import clock

    started = time.time()
    named = None
    if args.symbols or args.watchlist or args.universe:
        named = set(symbols_for(args))
    try:
        store = Prices(args.db)
        store.trading_days()
    except sqlite3.DatabaseError as err:
        _refuse(args, f"scan.py: {args.db} is not a readable price cache ({err}). Rebuild it "
                      f"with nse-backtest --fetch, or pass --live.")
        return 2
    try:
        # With no rule named, every rule -- the same as the US pack's daily scan.
        wanted = sorted(SETUPS) if (args.all or not args.setup) else [args.setup]

        if args.all or not args.setup:
            # Best record first, so the rule most worth reading is at the top and the ones that
            # lost money are at the bottom where they belong.
            wanted.sort(key=lambda s: -((store.record_for(s) or {}).get("edge") or -99))

        if not args.json and (args.min_price is not None or args.min_volume is not None):
            print("Note: --min-price and --min-volume are the --live scan's floors. The cached "
                  f"scan uses the replay's own {MIN_TURNOVER_LACS:g}-lakh turnover floor, so a "
                  "rule is screened on the universe it was measured on.\n")
        known = store.trading_days()
        shown_day = (args.on or known[-1]) if known else None
        behind = (_sessions_missing(shown_day, when or _dt.now(clock.IST))
                  if shown_day in known and not args.on else None)
        if not args.json and shown_day in known:
            # The time of the data, said first, as the US pack's "closes through" line says it:
            # a cache that stopped updating must never read as today's scan.
            print(f"Daily scan on the price cache, closes of {shown_day}"
                  + (f" (the latest cached day; clock {when:%a %Y-%m-%d %H:%M} IST)"
                     if when and not args.on else "") + ".")
            if behind:
                print(f"STALE: the cache ends {shown_day} and the session of {behind} is not in "
                      f"it, so these are {shown_day}'s closes, not the latest. Refresh it with "
                      f"python nse-backtest/backtest.py --fetch 5.")
            print()
        everything = {}
        for setup in wanted:
            try:
                rows = firing(store, setup, args.window, args.on)
            except SystemExit as err:
                if isinstance(err.code, str):
                    _refuse(args, f"scan.py: {err.code}")
                    return 2
                raise
            if named is not None:
                rows = [r for r in rows if r["symbol"] in named]
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
                       empty=f"   Nothing fired on {shown_day}.")
            if len(rows) > args.top:
                print(f"   ... and {len(rows) - args.top} more")
            print()

        if args.save and not args.no_write:
            saved = show.hand_off("scan.json", everything)
            if not args.json:
                print(f"wrote {saved}")

        # The watchlist the rest of the chain reads, as the US pack's daily scan writes it: one
        # hit per symbol, every rule that fired on it, most traded first.
        days = store.trading_days()
        day = args.on or days[-1]
        merged: dict[str, dict] = {}
        for setup, body in everything.items():
            for row in body["candidates"]:
                hit = merged.setdefault(row["symbol"], {
                    "symbol": row["symbol"], "setups": [], "price": row.get("close"),
                    "close_date": day, "change_percent": row.get("change_percent"),
                    "delivery_percent": row.get("delivery_percent"),
                    "turnover_lacs": row.get("turnover_lacs")})
                hit["setups"].append(setup)
        hits = sorted(merged.values(), key=lambda h: (-(h["turnover_lacs"] or 0), h["symbol"]))
        when = when or _dt.now(clock.IST)
        cached = store.db.execute("SELECT COUNT(DISTINCT symbol) FROM bars").fetchone()[0]
        scanned = len(store.day(day)) if named is None else len(
            [r for r in store.day(day) if r["symbol"] in named])
        doc = {"created_by": "nse-scan",
               "created_at": when.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
               "mode": "daily", "as_of": f"{day} close",
               "universe": cached if named is None else len(named),
               "scanned": scanned, "failed": 0,
               "source": "price cache (exchange bhavcopy)",
               "filters": {"min_turnover_lacs": MIN_TURNOVER_LACS, "window": args.window},
               "rules": {s: {"worth_acting_on": v["worth_acting_on"]}
                         for s, v in everything.items()},
               "note": NOTE, "hits": hits}
        if behind:
            # Said in the file too, so a reader of the watchlist never takes old closes as new.
            doc["stale"] = f"the cache ends {day}; the session of {behind} is not in it"
        where = None if args.no_write else show.hand_off("watchlist.json", doc, args.out)

        if args.json:
            counts = {"requested": doc["universe"], "scanned": scanned, "hits": len(hits),
                      "by_rule": {s: len(v["candidates"]) for s, v in everything.items()}}
            print(json.dumps(dict(doc, counts=counts, by_rule=everything,
                                  seconds=round(time.time() - started, 1)),
                             indent=2, default=str))
            return 0 if hits else 1

        # The US pack's count line: what was scanned, and how many matched each rule, in words.
        per = [(s, len(v["candidates"])) for s, v in everything.items() if v["candidates"]]
        print(f"{scanned:,} symbols traded on {day}; "
              + (f"{_plural(len(hits), 'chart')} matched a rule: "
                 + ", ".join(f"{n} {LABEL.get(s, s)}" for s, n in per) + "."
                 if hits else "no chart matched a rule."))
        usable = [s for s, v in everything.items() if v["worth_acting_on"]]
        if not usable:
            print("None of these rules has a record worth acting on. A candidate list is not a "
                  "reason to buy.")
        if where:
            print(f"Wrote {where} (mode daily, {_plural(len(hits), 'hit')}).")
        print(NOTE + " Nothing here decides anything. Size with /nse-risk, which can say no.")
        return 0 if hits else 1
    finally:
        store.close()


if __name__ == "__main__":
    show.run(main)
