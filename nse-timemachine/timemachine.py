#!/usr/bin/env python3
"""Time Machine. Replays the rest of the day on a plan ten thousand times and gives the odds.

It does NOT predict anything. It takes the plan from /nse-exit and asks a narrow, answerable
question: on the stock's own past sessions, how often did a move like this reach the target before
it reached the stop?

Two answers come back, and they are different things:

  THE REPLAYS   10,000 runs built by resampling real bars from the stock's last 60 sessions at the
                same time of day. These give odds of target-first, stop-first, or neither by the
                flat-by time.
  THE HISTORY   the plan's own rule -- the same opening range, the same stop rule, the same target
                in R -- run again on every one of those past sessions, each day with ITS OWN
                opening range and so its own stop. That is what actually happened, not a
                simulation, and it is the honest check on the replays.

Standard library only. Nothing is placed.
"""
from __future__ import annotations

import argparse
import functools
import json
import math
import random
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import bars, clock, settings, show                 # noqa: E402

REPLAYS = 10000
SEED = 19
NEAR_BARS = 2
FLAT_BY = "15:20"
OPEN, CLOSE = "09:15", "15:30"
OR_MINUTES = 15

# How far back the pool reaches. MEASURED on 2026-10-24: the free chart endpoint returned 60 SBIN
# sessions of five-minute bars (2026-07-29 to 2026-10-23, 75 bars each). One-minute bars stop at 8
# sessions and a 30-day request is refused, which is why the replay is five-minute only.
SPAN = "60d"

# Fewer full sessions than this and the pool is too thin to resample: twenty days of 10:15 bars is
# twenty bars to draw from at each step, and fewer starts to repeat the same few days.
MIN_SESSIONS = 20

# The first this many replays are kept whole, price after every bar, as the US pack keeps them.
SAMPLE_PATHS = 160

# A session shorter than the others is a half day, a feed gap or today, and none of those belongs
# in a pool used to judge a full afternoon. The length is MEASURED rather than assumed: the
# fullest day in the window is the yardstick, so a feed that returns 74 bars on a 75-bar day
# cannot make the skill reject every session there is.
FULL_ENOUGH = 0.95

STOP_RULES = ("auto", "or-mid", "or-low", "pct")

# An NSE symbol as the exchange writes it: capitals, digits, & and - (M&M, BAJAJ-AUTO). Anything
# else is refused before it becomes a file name or a URL, as the US pack refuses a bad ticker.
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")

# A bar whose close has passed on the clock but not yet in the feed's own last trade may still
# change. It waits this long past its close, as /nse-entry waits for it.
SETTLE = timedelta(seconds=60)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class NotEnough(Exception):
    """Too little, or too doubtful, to say anything. Better than an answer built on three days."""


@functools.lru_cache(maxsize=4096)
def _minutes(hhmm: str) -> int:
    """'10:15' -> 615. A clock time only: 10:60 or 24:00 is refused, never read as 11:00."""
    text = str(hhmm).strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", text):
        raise ValueError(text)
    hours, mins = (int(x) for x in text.split(":"))
    if hours > 23 or mins > 59:
        raise ValueError(text)
    return hours * 60 + mins


@functools.lru_cache(maxsize=4096)
def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _on_grid(hhmm: str, what: str) -> str:
    """A time on the five-minute grid that starts at 09:15, or a refusal saying why not."""
    try:
        at = _minutes(hhmm)
    except (ValueError, AttributeError, TypeError):
        raise NotEnough(f"{what} wants a time like 10:15, got {hhmm!r}")
    if (at - _minutes(OPEN)) % 5:
        raise NotEnough(f"{what} {hhmm} is not on the five-minute grid that starts at 09:15 "
                        f"(use a bar's close, like 10:15)")
    return _hhmm(at)


def days_of(symbol: str, interval: str = "5m", offline: str | None = None,
            at: datetime | None = None, held: list | None = None,
            dropped: dict | None = None) -> dict[str, list[dict]]:
    """Every closed bar of the last 60 sessions, grouped by day, oldest first.

    `at` is the clock. A bar that had not closed by then is dropped, so a run pinned to 10:15 never
    sees the 10:15 bar, which closes at 10:20. The newest bar is also held back while the feed's
    own last trade is still before its close, for up to a minute after it: the US pack takes the
    earlier of the clock and the feed's time for the same reason. `held`, when given, gets a line
    saying so. `dropped`, when given, counts the bars left out, as the US pack counts them: not
    closed, held back, and those the feed's reader threw away (no price, off the grid, outside
    hours, sent twice, no time).
    """
    rows = bars.candles(symbol, interval, offline=offline, span=SPAN)
    if dropped is not None:
        # What the feed's own reader threw away, as the US pack counts it: bars with no price,
        # off the five-minute grid, outside market hours, sent twice, or with no time.
        dropped.update({key: n for key, n in (bars.dropped_counts(symbol) or {}).items() if n})
    if at is not None:
        closed = [bar for bar in rows if bar["end"] <= at]
        if dropped is not None:
            dropped["not_complete"] = len(rows) - len(closed)
        rows = closed
        traded_at = bars.last_trade(symbol)
        if rows and traded_at is not None:
            newest = rows[-1]
            if traded_at < newest["end"] and at < newest["end"] + SETTLE:
                rows = rows[:-1]
                if dropped is not None:
                    dropped["held_back"] = 1
                if held is not None:
                    held.append((newest["start"].date().isoformat(),
                                 f"the feed's last trade is {traded_at:%H:%M:%S}, before the "
                                 f"{newest['start']:%H:%M} bar's close, so that bar may still "
                                 f"change: run it again in a minute"))
    days: dict[str, list[dict]] = {}
    for bar in rows:
        days.setdefault(bar["start"].date().isoformat(), []).append(bar)
    return dict(sorted(days.items()))


def sessions(symbol: str, before: str, interval: str = "5m", offline: str | None = None,
             days: dict[str, list[dict]] | None = None) -> list[list[dict]]:
    """Past sessions before the plan's date, oldest first, each with at least FULL_ENOUGH of the
    fullest day's bars.

    Not every one is complete: the feed leaves bars empty (SBIN's 15:20 bar on 57 of 60 saved
    days). A missing bar is never filled and never bridged: ratios() leaves out the pair around
    it, and pool() leaves out a time of day held by too few sessions. A half day or today is
    left out whole, since it would make the afternoon look shorter than it is.
    """
    days = days if days is not None else days_of(symbol, interval, offline)
    if not days:
        raise NotEnough(f"{symbol}: no intraday bars at all")
    longest = max(len(day) for day in days.values())
    whole = [day for when, day in days.items()
             if when < before and len(day) >= longest * FULL_ENOUGH]
    if not whole:
        raise NotEnough(f"{symbol}: no complete session before {before} in the data")
    return whole


def short_sessions(days: dict[str, list[dict]], before: str) -> list[dict]:
    """The days left out of the pool for being short, and how many bars each had, as the US pack
    lists them, so the output can name them."""
    if not days:
        return []
    longest = max(len(day) for day in days.values())
    return [{"date": when, "bars": len(day)} for when, day in days.items()
            if when < before and len(day) < longest * FULL_ENOUGH]


def missing_bars(days: dict[str, list[dict]], before: str) -> int:
    """How many 09:15 to 15:25 bars the feed sent nothing for, over the days before the plan's."""
    every = (_minutes(CLOSE) - _minutes(OPEN)) // 5
    return sum(every - len({b["start"] for b in day})
               for when, day in days.items() if when < before)


STEP = timedelta(minutes=5)


def _next_to(before: dict, bar: dict) -> bool:
    """Whether `bar` starts exactly one five-minute bar after `before`, with no hole between."""
    return bar["start"] - before["start"] == STEP


def ratios(day: list[dict]) -> list[dict]:
    """Each bar as four ratios to the close before it, so it can be rescaled to any price.

    Only a bar that starts exactly five minutes after the one before it. A hole is never bridged:
    MEASURED on the saved SBIN days, the 15:25 bar after an empty 15:20 is a ten-minute move, and
    filing it as a five-minute one lifted target-first from 15.4% to 20.6% on a 15:30 replay.
    """
    out = []
    for before, bar in zip(day, day[1:]):
        base = before["close"]
        if not base or not _next_to(before, bar):
            continue
        out.append({"at": bar["start"].strftime("%H:%M"),
                    "open": bar["open"] / base, "high": bar["high"] / base,
                    "low": bar["low"] / base, "close": bar["close"] / base})
    return out


def holes(history: list[list[dict]]) -> int:
    """How many bar pairs ratios() left out because a bar was missing between them."""
    return sum(1 for day in history for before, bar in zip(day, day[1:])
               if before["close"] and not _next_to(before, bar))


def slot_sizes(history: list[list[dict]]) -> dict[str, int]:
    """How many past sessions hold each time of day's bar, before any slot is dropped."""
    sizes: dict[str, int] = {}
    for day in history:
        for step in ratios(day):
            sizes[step["at"]] = sizes.get(step["at"], 0) + 1
    return dict(sorted(sizes.items()))


def pool(history: list[list[dict]], least: int = MIN_SESSIONS) -> dict[str, list[dict]]:
    """Past bars grouped by time of day. The market at 09:20 is not the market at 14:50.

    A time of day held by fewer than `least` sessions is left out. The draw picks a time slot
    first and a bar from it second, so a slot of 3 sessions would be drawn as often as one of 60
    and replay those 3 days over and over: measured, SBIN's 15:20 bar is in 3 of 60 saved days.
    """
    by_time: dict[str, list[dict]] = {}
    for day in history:
        for step in ratios(day):
            by_time.setdefault(step["at"], []).append(step)
    return {at: steps for at, steps in sorted(by_time.items()) if len(steps) >= least}


def _draw(when: str, order: list[str], by_time: dict[str, list[dict]], rng: random.Random,
          spread: int) -> dict | None:
    """One past bar for the bar at `when`: a time slot near it, then a bar from that slot.

    The US pack's draw, in its order: the slot is `when` moved by randint(-spread, spread) bars and
    kept inside the day (09:20 to 15:25, the bars that have a bar before them), then one bar is
    chosen from that slot's past sessions. Choosing the slot first gives each nearby time the same
    chance whatever number of sessions it holds.

    The move is counted on the clock, never along the slots the pool happens to hold: with a thin
    10:10 left out, counting along the pool would make "two bars back" from 10:15 the 10:00 bar,
    three bars away. A slot the pool left out is replaced by the nearest one it holds on the way
    back towards `when`, which always holds, so the draw never reaches past +-spread bars.
    `order` is unused and kept so callers need not change.
    """
    if when not in by_time:
        return None
    pick = when
    if spread:
        move = rng.randint(-spread, spread)
        at = min(_minutes(CLOSE) - 5, max(_minutes(OPEN) + 5, _minutes(when) + 5 * move))
        back = -5 if at > _minutes(when) else 5
        while _hhmm(at) not in by_time:
            at += back
        pick = _hhmm(at)
    return rng.choice(by_time[pick])


def walk(start: float, stop: float, target: float, times: list[str],
         by_time: dict[str, list[dict]], rng: random.Random,
         spread: int = NEAR_BARS, path: list | None = None) -> tuple[str, float, float]:
    """One run through the rest of the day: (outcome, R, the price at the last bar).

    The stop is checked before the target in every bar, so a bar that touches both counts as the
    stop: from a bar nobody can tell which came first, and assuming the good one flatters every
    number that follows. The walk goes on to the last bar after the outcome is known, so the price
    at --until is there for every replay, as in the US pack. A bar that opens through the stop
    still counts as -1R here; a real fill there can be worse.

    `path`, when given, gets the price after each bar, rounded to the paisa: the sample paths.
    """
    price, outcome = start, None
    order = list(by_time)
    for when in times:
        step = _draw(when, order, by_time, rng, spread)
        if step is None:
            continue
        low, high = price * step["low"], price * step["high"]
        if outcome is None:
            if low <= stop:
                outcome = "stop"
            elif high >= target:
                outcome = "target"
        price = price * step["close"]
        if path is not None:
            path.append(round(price, 2))
    risk = start - stop
    # The R paid at the target to four places, as the US pack scores it (its r_at_target).
    r = (-1.0 if outcome == "stop" else round((target - start) / risk, 4) if outcome == "target"
         else (price - start) / risk)
    return outcome or "neither", r, price


def one_replay(start: float, stop: float, target: float, times: list[str],
               by_time: dict[str, list[dict]], rng: random.Random,
               spread: int = NEAR_BARS) -> tuple[str, float]:
    """One run through the rest of the day, as (outcome, R). See walk()."""
    outcome, r, _ = walk(start, stop, target, times, by_time, rng, spread)
    return outcome, r


def percentile(ordered: list[float], q: float) -> float:
    """The US pack's percentile: the sorted list at index int(q * n)."""
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def bands(entry: float, times: list[str], by_time: dict[str, list[dict]], seed: int,
          spread: int, paths: int = 2000) -> list[list[float]]:
    """The 5th, 25th, 50th, 75th and 95th percentile of the price after each bar.

    A second run of 2,000 paths on the same seed, closes only, for the fan chart.
    """
    rng = random.Random(seed)
    order = list(by_time)
    every = []
    for _ in range(paths):
        price, path = entry, [entry]
        for when in times:
            step = _draw(when, order, by_time, rng, spread)
            if step is not None:
                price = price * step["close"]
            path.append(price)
        every.append(path)
    out = []
    for j in range(len(times) + 1):
        column = sorted(path[j] for path in every)
        out.append([round(percentile(column, q), 2) for q in (0.05, 0.25, 0.5, 0.75, 0.95)])
    return out


# ---------------------------------------------------------------- the plan

def checked(raw: dict) -> dict:
    """A plan (nse/plan.json shape, or built from flags) -> the checked plan the replay runs.

    Every refusal here is a plan that would otherwise replay as something it is not: a stop above
    the entry replays as a short, an entry off the bar grid starts the replay at the wrong bar.
    """
    given_symbol = raw.get("symbol")
    symbol = (given_symbol.strip().upper().removesuffix(".NS")
              if isinstance(given_symbol, str) else "")
    if not symbol and given_symbol in (None, ""):
        raise NotEnough("the plan needs a symbol")
    if not SYMBOL.match(symbol):
        raise NotEnough(f"the plan needs an NSE symbol like SBIN or BAJAJ-AUTO, got "
                        f"{given_symbol!r}")
    if raw.get("date"):
        try:
            raw = dict(raw, date=date.fromisoformat(str(raw["date"])).isoformat())
        except ValueError:
            # A date that is not YYYY-MM-DD compares as text against the pool's dates and lets
            # sessions from AFTER the plan into it: measured, "22-10-2026" put 23 October's
            # session into the replays of a 22 October plan.
            raise NotEnough(f"the plan needs a date like 2026-10-22, got {raw['date']!r}")
    side = str(raw.get("side") or "buy").strip().lower()
    if side in ("sell", "short"):
        raise NotEnough("this replays buys only, as the rest of this pack plans them. A short on "
                        "NSE has to be squared off the same day (MIS), and none of the skills "
                        "before this one plans one")
    if side not in ("buy", "long"):                  # nse-exit reads "long" as a buy too
        raise NotEnough(f"side must be buy, got {raw.get('side')!r}")
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise NotEnough(f"five-minute bars only (the plan says {interval}): the free feed keeps 60 "
                        f"sessions of five-minute bars but only 8 of one-minute bars, too few for "
                        f"a fair pool")
    try:
        if any(isinstance(raw.get(k), bool) for k in ("entry", "stop", "target")):
            raise TypeError("a true/false is not a price")
        entry, stop, target = (float(raw[k]) for k in ("entry", "stop", "target"))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise NotEnough("the plan needs an entry, a stop and a target, each a price")
    if not all(math.isfinite(v) for v in (entry, stop, target)):
        raise NotEnough("the plan's entry, stop and target must each be a number")
    if min(entry, stop, target) <= 0:
        raise NotEnough("the plan's entry, stop and target must all be positive prices")
    if not stop < entry:
        raise NotEnough(f"the stop {stop:,.2f} is not below the entry {entry:,.2f}; this replays "
                        f"long trades only")
    if not target > entry:
        raise NotEnough(f"the target {target:,.2f} is not above the entry {entry:,.2f}")

    window = raw.get("opening_range")
    # `is None`, never `or`: a range of 0 minutes is a mistake to refuse, not a 15 to assume.
    minutes = window.get("minutes") if isinstance(window, dict) else None
    if minutes is None:
        minutes = raw.get("or_minutes")
    if minutes is None:
        minutes = OR_MINUTES
    try:
        # A whole number only: int(20.9) is 20, and a range of 20.9 minutes read as 20 is a
        # different range from the one the plan was made with.
        if isinstance(minutes, bool) or float(minutes) != int(float(minutes)):
            raise ValueError(minutes)
        minutes = int(float(minutes))
    except (TypeError, ValueError, OverflowError):
        raise NotEnough(f"the opening range must be a whole number of minutes, got {minutes!r}")
    if minutes < 5 or minutes % 5 or minutes >= _minutes(CLOSE) - _minutes(OPEN):
        raise NotEnough(f"the opening range must be a multiple of 5 minutes, got {minutes}")

    entry_time = raw.get("entry_time")
    if entry_time:
        entry_time = _on_grid(entry_time, "the entry time")
    # entry_bar is this pack's bar number, as /nse-entry counts it and /nse-exit writes it: 1 is
    # the 09:15 bar, which closes 09:20, so bar n closes at 09:15 + 5n minutes.
    if raw.get("entry_bar") is not None:
        given = raw["entry_bar"]
        try:
            if isinstance(given, bool) or float(given) != int(float(given)):
                raise ValueError(given)
            number = int(float(given))
        except (TypeError, ValueError, OverflowError):
            raise NotEnough(f"entry_bar must be a bar number, got {given!r}")
        if number < 1:
            raise NotEnough(f"entry_bar must be 1 or more (1 is the 09:15 bar), got {number}")
        from_bar = _hhmm(_minutes(OPEN) + 5 * number)
        if entry_time and entry_time != from_bar:
            raise NotEnough(f"the plan disagrees with itself: entry_bar {number} closes at "
                            f"{from_bar} but entry_time is {entry_time}")
        entry_time = from_bar
    if not entry_time:
        # Refused, as the US pack refuses it: assuming the entry was at the end of the range would
        # replay a trade that may never have been taken at that bar.
        raise NotEnough("the plan needs an entry time, the entry bar's close (like 10:15), or "
                        "entry_bar: give --entry-time, or replay the plan /nse-exit wrote after "
                        "/nse-entry fired")
    if not _minutes(OPEN) + 5 <= _minutes(entry_time) <= _minutes(CLOSE) - 5:
        raise NotEnough(f"the entry must close between 09:20 and 15:25 to leave bars to replay "
                        f"(got {entry_time})")

    rule = str(raw.get("stop_rule") or "auto").strip().lower().replace("_", "-")
    if rule.startswith(("pct", "price:", "atr:")):
        rule = "pct"                       # a fixed stop distance: replayed as that percentage
    elif rule not in STOP_RULES:
        raise NotEnough(f"{raw.get('stop_rule')!r} is not a stop rule the history can rebuild; "
                        f"use {', '.join(STOP_RULES)}")

    risk = entry - stop
    return {
        "symbol": symbol, "side": "buy", "date": raw.get("date"),
        "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": entry_time,
        "entry_bar": (_minutes(entry_time) - _minutes(OPEN)) // 5,
        "stop": stop, "target": target,
        "risk_per_share": round(risk, 2), "target_r": round((target - entry) / risk, 2),
        "opening_range": {"minutes": minutes}, "stop_rule": rule, "interval": "5m",
        "flat_by": raw.get("flat_by") or FLAT_BY,
    }


def _opening_range(day: list[dict], minutes: int) -> tuple[float, float] | None:
    """(high, low) of a session's first `minutes`, or None unless every one of those bars is there.

    By time, not by position: a session missing its 09:20 bar would otherwise hand the 09:30 bar
    to the range and give that day a range it never had.
    """
    ends = _minutes(OPEN) + minutes
    inside = [b for b in day if _minutes(b["end"].strftime("%H:%M")) <= ends]
    if len(inside) != minutes // 5 or inside[0]["start"].strftime("%H:%M") != OPEN:
        return None
    return max(b["high"] for b in inside), min(b["low"] for b in inside)


def resolve_rule(plan: dict, entry_day: list[dict]) -> tuple[dict | None, str | None]:
    """The rule the history replays, and a note when it had to fall back.

    orb, the plan's opening range, its stop rule and its target in R. The stop rule is or-mid (the
    range's midpoint), or-low (its low), or pct (the plan's stop distance as a percentage of the
    entry). 'auto' works it out from the plan day's own range: a stop that IS that day's midpoint
    or low, to the tick, was set by that rule. None when the setup is not orb, because the history
    knows how to find an orb entry on a past day and nothing else.
    """
    if plan["setup"].lower() != "orb":
        return None, (f"skipped: the history replays the opening-range breakout (orb) only, and "
                      f"this plan's setup is {plan['setup']!r}")
    minutes = plan["opening_range"]["minutes"]
    rule = {"setup": "orb", "or_minutes": minutes, "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    note = None
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        window = _opening_range(entry_day, minutes) if entry_day else None
        if window:
            high, low = window
            if abs(clock.to_tick((high + low) / 2, up=False) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or-mid"
            elif abs(clock.to_tick(low, up=False) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or-low"
        else:
            note = (f"the plan's own day {plan['date']} has no complete opening range in the "
                    f"data, so its stop could not be matched to one; the history uses the same "
                    f"stop distance as a percentage of each day's entry")
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule, note


def rule_stop(rule: dict, high: float, low: float, entry: float) -> float:
    """One past day's stop, from that day's own opening range, rounded to a tick away from entry."""
    if rule["stop_rule"] == "or-mid":
        return clock.to_tick((high + low) / 2, up=False)
    if rule["stop_rule"] == "or-low":
        return clock.to_tick(low, up=False)
    return clock.to_tick(entry * (1 - rule["stop_pct"] / 100.0), up=False)


def history_replay(history: list[list[dict]], rule: dict, until: str) -> dict:
    """The plan's rule on every past full session. Not a simulation: this is what the days did.

    Entry is the first bar after the opening range to close above it, up to the bar closing at
    --until. Then bar by bar, the stop first and the target second (a bar touching both is the
    stop), else the close of the --until bar.

    A bar the feed never sent is never stepped over. Missing before the break, the first close
    above the range may have been in it; missing after the entry, the stop or the target may have
    been touched in it; missing at --until, the price there is not known. Each such day is left
    out of the counts and named under `left_out`, never scored on the bars around the hole.
    MEASURED on the saved SBIN days: 6 of them have no 15:15 bar, and scoring those on the 15:10
    close called a 15:10 price "still open at 15:20".
    """
    minutes = rule["or_minutes"]
    start = _minutes(OPEN) + minutes
    slots = [_hhmm(m) for m in range(start, _minutes(until), 5)]     # bar starts, range to --until
    trades, left_out = [], []
    for day in history:
        when = day[0]["start"].date().isoformat()
        window = _opening_range(day, minutes)
        if not window:
            left_out.append({"date": when, "why": "its opening range is missing a bar"})
            continue
        high, low = window
        by_start = {b["start"].strftime("%H:%M"): b for b in day}
        live, hole = [], None
        for at in slots:
            bar = by_start.get(at)
            if bar is None:
                hole = at
                break
            live.append(bar)
            if bar["close"] > high:
                break
        if hole is not None:
            left_out.append({"date": when, "why": f"no {hole} bar before the range broke"})
            continue
        broke = len(live) - 1 if live and live[-1]["close"] > high else None
        if broke is None:
            continue
        entry = live[broke]["close"]
        stop = rule_stop(rule, high, low, entry)
        risk = entry - stop
        if risk <= 0:
            continue
        target = clock.to_tick(entry + rule["target_r"] * risk, up=True)
        outcome = None
        for at in slots[broke + 1:]:
            bar = by_start.get(at)
            if bar is None:
                hole = at
                break
            live.append(bar)
            if bar["low"] <= stop:
                outcome = "stop"
                break
            if bar["high"] >= target:
                outcome = "target"
                break
        if hole is not None:
            left_out.append({"date": when, "why": f"no {hole} bar while the trade was open"})
            continue
        if outcome == "target":
            r = (target - entry) / risk
        elif outcome == "stop":
            r = -1.0
        else:
            r = (live[-1]["close"] - entry) / risk
        trades.append({"date": day[0]["start"].date().isoformat(),
                       "time": live[broke]["end"].strftime("%H:%M"),
                       "entry": round(entry, 2), "stop": stop, "target": target,
                       "outcome": outcome or "close", "R": round(r, 2)})
    return {
        "sessions": len(history), "triggered": len(trades),
        "target": sum(1 for t in trades if t["outcome"] == "target"),
        "stop": sum(1 for t in trades if t["outcome"] == "stop"),
        "close": sum(1 for t in trades if t["outcome"] == "close"),
        "green": sum(1 for t in trades if t["R"] > 0),
        # statistics.mean, as the US pack averages: fmean rounds differently in the last place,
        # and measured, it moved 9 of 768 history averages by 0.001 against the US pack's.
        "avg_R": round(statistics.mean(t["R"] for t in trades), 3) if trades else None,
        "rule": rule, "trades": trades, "left_out": left_out,
    }


# ---------------------------------------------------------------- the run

def replay(plan: dict, runs: int = REPLAYS, seed: int = SEED, until: str | None = None,
           offline: str | None = None, spread: int = NEAR_BARS,
           at: datetime | None = None) -> dict:
    """The odds from resampled real bars, and the plan's rule on the real past days."""
    at = at or clock.now()
    if not 1 <= runs <= 1_000_000:
        raise NotEnough("--replays must be between 1 and 1,000,000")
    if not 0 <= spread <= 10:
        raise NotEnough("--window must be between 0 and 10 bars")
    plan = checked(plan)
    plan["date"] = plan["date"] or at.date().isoformat()
    symbol, entry = plan["symbol"], plan["entry"]
    stop, target = plan["stop"], plan["target"]

    # Named for where it came from: a bad flat_by in the plan is the plan's mistake, and saying
    # "--until" about a flag the user never typed sends them looking in the wrong place.
    # `is None`, never `or`: an empty --until is a mistake to refuse, not the flat-by to assume.
    what = "--until" if until is not None else "the plan's flat_by"
    until = _on_grid(plan["flat_by"] if until is None else until, what)
    if _minutes(until) > _minutes(CLOSE):
        raise NotEnough(f"{what} {until} is after the 15:30 close")
    if _minutes(until) <= _minutes(plan["entry_time"]):
        raise NotEnough(f"{what} {until} leaves nothing to replay after the "
                        f"{plan['entry_time']} entry")

    held: list[str] = []
    unused = {"not_complete": 0, "held_back": 0}
    days = days_of(symbol, "5m", offline, at, held, unused)

    # The entry first and the pool second, in the US pack's order: a plan for a day the data does
    # not have is told that, not that there is too little history before it.
    warnings = []
    entry_day = days.get(plan["date"], [])
    if entry_day:
        bar = next((b for b in entry_day if b["end"].strftime("%H:%M") == plan["entry_time"]),
                   None)
        if bar is None:
            last = entry_day[-1]
            if held and held[0][0] == plan["date"]:
                why = held[0][1]
            elif last["end"].strftime("%H:%M") > plan["entry_time"]:
                # The day goes on past the entry, so the bar is a hole in the feed, not the end
                # of the data: "the data stops at 15:25" would be false.
                why = (f"the feed sent no "
                       f"{_hhmm(_minutes(plan['entry_time']) - 5)} bar, though it has later bars "
                       f"that day, up to the {last['start'].strftime('%H:%M')} bar")
            else:
                why = (f"the data stops at the {last['start'].strftime('%H:%M')} bar, closing "
                       f"{last['end'].strftime('%H:%M')}")
            raise NotEnough(f"the entry is not in the data: no bar of {symbol} closing at "
                            f"{plan['entry_time']} on {plan['date']} ({why})")
        if not bar["low"] - 0.01 <= entry <= bar["high"] + 0.01:
            warnings.append(f"the plan's entry {entry:,.2f} is outside that bar's range "
                            f"{bar['low']:,.2f} to {bar['high']:,.2f}")
    else:
        why = f" ({held[0][1]})" if held and held[0][0] == plan["date"] else ""
        weekday = WEEKDAYS[date.fromisoformat(plan["date"]).weekday()]
        raise NotEnough(f"the entry is not in the data: no bars of {symbol} on "
                        f"{plan['date']}, a {weekday}{why}. Replay a day the feed has, after its entry bar "
                        f"has closed")

    history = sessions(symbol, plan["date"], "5m", offline, days)
    if len(history) < MIN_SESSIONS:
        raise NotEnough(f"only {len(history)} full session(s) of {symbol} before {plan['date']} "
                        f"in the data; the time machine needs at least {MIN_SESSIONS}")

    sizes = slot_sizes(history)
    by_time = pool(history)
    dropped = {at: n for at, n in sizes.items() if at not in by_time}
    # Every bar from the entry to --until, on the grid. Each must be in the pool: a bar with too
    # few sessions behind it is refused by name, never skipped, because skipping it would replay
    # a shorter afternoon than the plan has.
    times = [_hhmm(m) for m in range(_minutes(plan["entry_time"]), _minutes(until), 5)]
    for when in times:
        if when not in by_time:
            raise NotEnough(f"the {when} bar is in only {sizes.get(when, 0)} of the "
                            f"{len(history)} past sessions (with the bar before it, no hole "
                            f"between), under the {MIN_SESSIONS} a fair pool needs. Replay to an "
                            f"earlier --until")
    skipped = holes(history)

    rng = random.Random(seed)
    counts = {"target": 0, "stop": 0, "neither": 0}
    rs, green, closes, samples = [], 0, [], []
    for n in range(runs):
        path = [entry] if n < SAMPLE_PATHS else None
        how, r, price = walk(entry, stop, target, times, by_time, rng, spread, path)
        if path is not None:
            samples.append(path)
        counts[how] += 1
        rs.append(r)
        closes.append(price)
        if how == "target" or (how == "neither" and price > entry):
            green += 1
    ordered = sorted(closes)
    low, high = percentile(ordered, 0.01), percentile(ordered, 0.99)
    width = (high - low) / 24
    histogram = [0] * 24
    for price in ordered:
        slot = int((price - low) / width) if width > 0 else 0
        if 0 <= slot < 24:
            histogram[slot] += 1

    rule, note = resolve_rule(plan, entry_day)
    if rule:
        plan["stop_rule"] = rule["stop_rule"]
        history_found = history_replay(history, rule, until)
        if note:
            history_found["note"] = note
    else:
        history_found = {"sessions": len(history), "triggered": 0, "rule": None, "trades": [],
                         "note": note}

    decided = counts["target"] + counts["stop"]
    # The target's R to four places, as the US pack carries it into its verdict.
    reward = round((target - entry) / (entry - stop), 4)
    avg_r = round(statistics.mean(rs), 3)                  # mean, as the US pack: see above
    newest = max((b for day in days.values() for b in day), key=lambda b: b["start"])
    return {
        "created_by": "nse-timemachine",
        "created_at": at.isoformat(timespec="seconds"),
        "source": "offline" if offline else "yahoo",
        # Which bars these are and how fresh: the last bar the run could use, so a saved result
        # read later says what it was built on.
        "data": {"symbol": symbol, "interval": "5m", "range": SPAN,
                 "offline": str(offline) if offline else None,
                 "last_bar": f"{newest['start']:%Y-%m-%d %H:%M} IST",
                 "held_back": [text for _, text in held],
                 # What was left out of the bars, as the US pack counts it: not closed by the
                 # clock, held back while the feed was behind, and the bars the feed sent
                 # nothing for on the days before the plan's.
                 "dropped": dict(unused, missing_in_past_sessions=missing_bars(
                     days, plan["date"]))},
        "plan": plan,
        "symbol": symbol, "runs": runs, "seed": seed, "until": until, "window": spread,
        "entry": entry, "stop": stop, "target": target, "reward_r": round(reward, 2),
        "r_at_target": reward,
        "entry_time": plan["entry_time"], "bars_left": len(times),
        "sessions_used": len(history),
        "complete_sessions": sum(1 for day in history
                                 if len(day) == max(len(d) for d in days.values())),
        "pairs_skipped_for_holes": skipped,
        "slots_left_out": dropped,
        "pool_from": history[0][0]["start"].date().isoformat(),
        "pool_to": history[-1][0]["start"].date().isoformat(),
        "short_sessions_left_out": short_sessions(days, plan["date"]),
        "target_first_percent": round(counts["target"] / runs * 100, 1),
        "stop_first_percent": round(counts["stop"] / runs * 100, 1),
        "neither_percent": round(counts["neither"] / runs * 100, 1),
        "green_percent": round(green / runs * 100, 1),
        "median_r": round(statistics.median(rs), 2),
        "mean_r": round(statistics.mean(rs), 2),
        "counts": dict(counts, green=green),
        "decided": decided,
        "target_share_of_decided": (round(counts["target"] / decided * 100, 1)
                                    if decided else None),
        "break_even_percent": round(100 / (1 + reward), 1),
        "verdict": verdict(counts, decided, reward, until),
        "verdict_notes": verdict_notes(counts, decided, reward, runs, avg_r, until),
        "median_close": round(percentile(ordered, 0.5), 2),
        "p5_close": round(percentile(ordered, 0.05), 2),
        "p95_close": round(percentile(ordered, 0.95), 2),
        "avg_R": avg_r,
        "bands": bands(entry, times, by_time, seed, spread),
        "band_paths": 2000,
        "band_times": [plan["entry_time"]] + [
            f"{(_minutes(t) + 5) // 60:02d}:{(_minutes(t) + 5) % 60:02d}" for t in times],
        "hist": {"lo": round(low, 2), "hi": round(high, 2), "bins": histogram},
        "sample_paths": samples,
        "history": history_found,
        "warnings": warnings,
        "note": (f"A simulation built from {symbol}'s own last {len(history)} sessions. It is "
                 f"not a prediction: results, news, a circuit or a halt can do things those "
                 f"sessions never did. Same seed, same numbers."),
    }


def verdict(counts: dict, decided: int, reward: float, until: str | None = None) -> str:
    """One line, and it is about break-even rather than about winning.

    A bigger target needs to win less often to break even: 1 in (1 + R). At 2R that is 33%, so a
    plan whose target comes first 32% of the time it is decided is NOT a loser, it is level. That
    distinction is the whole point of saying it this way. The US pack's thresholds and its two
    shapes: at 1R the two odds side by side, at any other R what break-even asks against what
    the replays did.
    """
    if not decided:
        by = until or "the flat-by time"
        return (f"No replay reached the target or the stop by {by}: nothing was decided, and "
                f"the price at {by} decides every one")
    runs = sum(counts.values()) or decided
    share = counts["target"] / decided * 100
    need = 100 / (1 + reward)
    gap = share - need
    one_r = abs(reward - 1) < 0.005
    if abs(gap) < 5:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(gap) < 15:
        head = "Leans toward the target" if gap > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if gap > 0 else "Strongly toward the stop"
    odds = (f"target first {counts['target'] / runs * 100:.1f}% vs stop first "
            f"{counts['stop'] / runs * 100:.1f}%")
    if one_r:
        return f"{head}: {odds}"
    return (f"{head}: at {round(reward, 2):g}R the target has to come first in {need:.0f}% of "
            f"the replays that hit one side, which is break-even; it did in {share:.0f}% "
            f"({odds})")


def verdict_notes(counts: dict, decided: int, reward: float, runs: int, mean_r: float,
                  until: str) -> list[str]:
    """The lines printed under the verdict, so nothing reads as one-sided. The US pack's two."""
    notes = []
    neither = counts["neither"] / runs if runs else 0
    if neither > 0.40 and decided:
        notes.append(f"{neither * 100:.1f}% of the replays hit neither by {until}; the price at "
                     f"{until} decides those.")
    if decided:
        gap = counts["target"] / decided - 1 / (1 + reward)
        if abs(gap) >= 0.05 and mean_r and (mean_r > 0) != (gap > 0):
            notes.append(f"Average R is {mean_r:+.3f} all the same: the replays that hit neither "
                         f"pull it the other way.")
    return notes


def fan_chart(found: dict, height: int = 17) -> list[str]:
    """The US pack's ASCII fan chart: one column per five-minute bar, the median and the bands."""
    plan, every = found["plan"], found.get("bands") or []
    if not every:
        return ["No bands in this result: run timemachine.py run --write again."]
    entry, stop, target = found["entry"], found["stop"], found["target"]
    lo = min([b[0] for b in every] + [stop, target])
    hi = max([b[4] for b in every] + [stop, target])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))

    grid = [[" "] * len(every) for _ in range(height)]
    for j, band in enumerate(every):
        r5, r25, r50, r75, r95 = (row(x) for x in band)
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(target): "target", row(stop): "stop"}
    if row(entry) not in marks:
        marks[row(entry)] = "entry"
    for r in (row(target), row(stop)):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = [f"{found['symbol']} {plan.get('date')}, {found.get('band_paths', 2000):,} replay paths "
           f"from the {found.get('entry_time')} entry (seed {found['seed']})"]
    for r in range(height):
        label = marks.get(r)
        price = {"target": target, "stop": stop, "entry": entry}.get(label, hi - r * step)
        left = f"{price:9.2f} |" if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + (f"  {label}" if label else ""))
    times = found.get("band_times") or []
    axis = [" "] * len(every)
    for j in sorted({0, len(every) // 2, len(every) - 1}):
        if j < len(times):
            text = times[j]
            start = max(0, min(j, len(every) - len(text)))
            axis[start:start + len(text)] = list(text)
    out.append("          +" + "-" * len(every))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90% of the paths   - stop and target")
    return out


def fan(found: dict, width: int = 48) -> list[str]:
    """A plain bar chart of the three outcomes. No colour, no library."""
    rows = [("target first", found["target_first_percent"], ""),
            ("stop first", found["stop_first_percent"], ""),
            ("neither", found["neither_percent"], f"  still open at {found['until']}")]
    out = []
    for name, pct, after in rows:
        filled = int(round(pct / 100 * width))
        out.append(f"  {name:<13} {'#' * filled}{'.' * (width - filled)} {pct:>5.1f}%{after}")
    return out


def inline_plan(args) -> dict:
    """A plan given as flags, so a replay does not need /nse-exit to have run first.

    Checked the same way a written plan is. A target can be a price or a multiple of the risk,
    like 2R; a multiple is rounded UP to a tick, as /nse-exit rounds one.
    """
    entry, stop = float(args.entry), float(args.stop)
    if min(entry, stop) <= 0:
        raise NotEnough("the plan's entry, stop and target must all be positive prices")
    given = getattr(args, "target", None)
    raw = str("2R" if given is None else given).strip().upper()
    try:
        if raw.endswith("R"):
            if stop >= entry:
                raise NotEnough(f"the stop {stop:,.2f} is not below the entry {entry:,.2f}; this "
                                f"replays long trades only")
            times = float(raw[:-1] or 1)
            if not math.isfinite(times) or times <= 0:
                raise ValueError(raw)
            target = clock.to_tick(entry + times * (entry - stop), up=True)
        else:
            target = float(raw)
        if not math.isfinite(target):
            raise ValueError(raw)
    except ValueError:
        raise NotEnough(f"the target must be a price or a multiple like 2R, got {raw!r}")
    # The flat-by /nse-exit would have written: --flat-by, else the config's flat_by, else 15:20.
    # Without the config a user flat by 15:00 had replayed a flags plan to 15:20, and the same
    # numbers by flags and by file no longer replayed alike.
    flat_by = getattr(args, "flat_by", None)
    what = "--flat-by"
    if flat_by is None:
        try:
            flat_by = settings.load(getattr(args, "config", None)).get("flat_by")
        except (OSError, ValueError) as err:          # a --config that is missing or not JSON
            raise NotEnough(str(err))
        what = "flat_by in the config"
    if flat_by is None:
        flat_by = FLAT_BY
    flat_by = _on_grid(flat_by, what)
    if _minutes(flat_by) > _minutes(CLOSE):
        raise NotEnough(f"{what} {flat_by} is after the 15:30 close")
    return checked({
        "symbol": args.symbol, "side": getattr(args, "side", None) or "buy",
        "entry": entry, "stop": stop, "target": target, "date": args.date,
        "entry_time": args.entry_time, "flat_by": flat_by, "interval": "5m",
        "setup": args.setup or "orb", "or_minutes": getattr(args, "or_minutes", None),
        "stop_rule": getattr(args, "stop_rule", None) or "auto",
    })


def report(found: dict) -> list[str]:
    plan, past = found["plan"], found["history"]
    weekday = WEEKDAYS[date.fromisoformat(plan["date"]).weekday()]
    lines = [
        f"{found['symbol']} buy, {weekday} {plan['date']}: entry Rs {found['entry']:,.2f} at "
        f"{found['entry_time']} (bar {plan['entry_bar']}), stop Rs {found['stop']:,.2f}, target "
        f"Rs {found['target']:,.2f} ({plan['target_r']:g}R, risk Rs "
        f"{plan['risk_per_share']:,.2f} a share).",
        f"{found['runs']:,} replays of the rest of the session, {found['entry_time']} to "
        f"{found['until']} ({found['bars_left']} five-minute bars), seed {found['seed']}.",
        f"Each bar is drawn from {found['symbol']}'s own bars at the same time of day "
        f"(+-{found['window']} bars) over its last {found['sessions_used']} sessions "
        f"({found['pool_from']} to {found['pool_to']}), {found['complete_sessions']} of them "
        f"with every bar.",
        f"{found['pairs_skipped_for_holes']} bar pair(s) with a missing bar between them were "
        f"left out, never bridged"
        + ("; times of day held by fewer than " + str(MIN_SESSIONS) + " sessions left out of "
           "the draw: " + ", ".join(f"{t} ({n})" for t, n in found["slots_left_out"].items())
           if found.get("slots_left_out") else "") + ".", ""]
    lines += fan(found)
    lines += [f"\n  green (target first, or above the entry at {found['until']}): "
              f"{found['green_percent']:.1f}%",
              f"\n  Price at {found['until']}: median Rs {found['median_close']:,.2f}, 90% of "
              f"replays between Rs {found['p5_close']:,.2f} and Rs {found['p95_close']:,.2f}.",
              f"  Average R: {found['avg_R']:+.3f} (R = the Rs {plan['risk_per_share']:,.2f} "
              f"risk: +{found.get('r_at_target', plan['target_r']):g}R at the target, -1R at "
              f"the stop). Median "
              f"{found['median_r']}R. A bar that opens through the stop still counts -1R here; "
              f"a real fill can be worse."]
    if past.get("rule"):
        rule = past["rule"]
        how = {"or-mid": "the opening-range midpoint", "or-low": "the opening-range low",
               "pct": f"{rule.get('stop_pct', 0):g}% under the entry"}[rule["stop_rule"]]
        gone = past.get("left_out") or []
        scored = past["sessions"] - len(gone)
        said = (f"\nWhat actually happened: the same rule ({rule['or_minutes']}-minute range, "
                f"first close above it, stop at {how}, target {rule['target_r']:g}R, out by "
                f"{found['until']}) on each of the {past['sessions']} sessions, each with its "
                f"own range")
        if gone:
            said += (f" ({len(gone)} left out for a bar the feed never sent before the trade "
                     f"was decided: " + ", ".join(f"{g['date']} {g['why']}" for g in gone)
                     + f"; {scored} scored)")
        if past["triggered"]:
            said += (f". It triggered {past['triggered']} time(s): target first {past['target']}, "
                     f"stop first {past['stop']}, still open at {found['until']} "
                     f"{past['close']}; {past['green']} green, average R {past['avg_R']:+.3f}.")
        else:
            said += f". It never triggered in those {scored} sessions."
        lines.append(said)
    if past.get("note"):
        lines.append(f"\nHistory: {past['note']}.")
    # The verdict after the history, as the US pack prints it, and the lines under it as they are.
    lines.append(f"\n{found['verdict']}.")
    lines += list(found.get("verdict_notes") or [])
    if found["short_sessions_left_out"]:
        short = found["short_sessions_left_out"]
        lines.append(f"\nLeft out {len(short)} short session(s): "
                     f"{', '.join(s['date'] for s in short)}.")
    for warning in found["warnings"]:
        lines.append(f"\nWarning: {warning}.")
    lines.append(f"\n{found['note']} Repeat this run with --seed {found['seed']}. Nothing was "
                 f"placed.")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=("run", "chart"),
                    help="run: replay the plan; chart: the ASCII fan chart")
    ap.add_argument("--plan", help="the plan to replay (default nse/plan.json)")
    ap.add_argument("--symbol", help="or give the plan as flags: the symbol")
    ap.add_argument("--side", default="buy",
                    help="buy (the only side this pack plans; sell is refused with the reason)")
    ap.add_argument("--entry", type=show.number, help="the entry price")
    ap.add_argument("--stop", type=show.number, help="the stop price")
    ap.add_argument("--target", help="the target: a price, or 2R / 1R / 1.5R")
    ap.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, IST); only "
                                   "sessions before it go into the pool")
    ap.add_argument("--entry-time", help="the entry bar's close, HH:MM; required with --symbol "
                                         "(there is no default)")
    ap.add_argument("--flat-by", help="out by this time, HH:MM (default: config flat_by, 15:20)")
    ap.add_argument("--config", help="the config whose flat_by a plan given as flags uses "
                                     "(default config.json or nse/config.json)")
    ap.add_argument("--setup", help="the plan's setup (default orb)")
    ap.add_argument("--or-minutes", type=int, default=None,
                    help="the opening range in minutes (default 15)")
    ap.add_argument("--stop-rule", default="auto",
                    choices=STOP_RULES + ("or_mid", "or_low"),
                    help="how the history sets each past day's stop: auto works it out from the "
                         "plan day's own range")
    ap.add_argument("--replays", "--runs", dest="runs", type=int, default=REPLAYS)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--window", type=int, default=NEAR_BARS,
                    help="draw from the same time of day, plus or minus this many bars")
    ap.add_argument("--until", help="end each replay at the bar closing here, like 15:20")
    ap.add_argument("--offline", help="a saved chart JSON, or a folder holding SYMBOL-60d5m.json")
    ap.add_argument("--now", help="pin the clock, like '2026-10-22 10:15' (IST)")
    ap.add_argument("--from", dest="from_file", nargs="?", const="nse/timemachine.json",
                    metavar="PATH", help="chart: draw a saved result instead of running again")
    ap.add_argument("--write", nargs="?", const="", metavar="PATH",
                    help="write the result (default nse/timemachine.json)")
    ap.add_argument("--out", help="write the result here (the same as --write PATH)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.mode:
        # As the US pack does with no command: the help, and nothing run.
        ap.print_help()
        return 1
    # The US pack gives each command its own flags; one parser here, so a flag the command does
    # not use is refused by name rather than silently dropped.
    if args.mode == "run" and args.from_file is not None:
        print("Cannot replay: --from draws a saved result and goes with chart, not run.")
        return 2
    if args.mode == "chart" and args.json:
        print("Cannot replay: --json goes with run; chart draws the fan chart.")
        return 2

    if args.mode == "chart" and args.from_file:
        path = Path(args.from_file)
        if not path.exists():
            print(f"No {path} yet. Run timemachine.py run --write.")
            return 2
        try:
            found = json.loads(path.read_text(encoding="utf-8"))
            # Drawn in full before anything is printed: a damaged result is refused in one line,
            # never half a chart and a traceback.
            drawn = fan_chart(found) if found["bands"] else None
            verdict_line = found.get("verdict") or ""
            notes = found.get("verdict_notes") or []
            # Text where text belongs: a string of notes would print one letter a line.
            if not isinstance(verdict_line, str) or not isinstance(notes, list) \
                    or not all(isinstance(n, str) for n in notes) \
                    or not isinstance(found.get("band_times", []), list):
                raise TypeError("not a result")
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError,
                OverflowError):
            drawn = None
        if not drawn:
            print(f"{path} is not a time machine result with bands: run it again with --write.")
            return 2
        print("\n".join(drawn))
        print("\n".join([verdict_line + ("." if verdict_line else "")] + notes))
        return 0

    try:
        at = clock.now(args.now)
    except ValueError as err:
        print(f"Cannot replay: {err}")
        return 2

    try:
        inline = [args.symbol, args.entry, args.stop]
        if any(v is not None for v in inline):
            if any(v is None for v in inline):
                print("An inline plan needs --symbol, --entry and --stop together.")
                return 2
            plan = inline_plan(args)
        else:
            # These describe a plan given as flags. With a written plan they would be silently
            # ignored, and the run would replay a different plan from the one the user typed.
            loose = [flag for flag, value in (("--target", args.target), ("--date", args.date),
                                              ("--entry-time", args.entry_time),
                                              ("--flat-by", args.flat_by),
                                              ("--setup", args.setup),
                                              ("--config", args.config)) if value is not None]
            if loose:
                raise NotEnough(f"{', '.join(loose)} "
                                f"{'describes' if len(loose) == 1 else 'describe'} a plan "
                                f"given as flags and {'goes' if len(loose) == 1 else 'go'} with "
                                f"--symbol --entry --stop; a written plan carries its own (use "
                                f"--until to end the replay earlier)")
            if args.side.lower() != "buy":
                raise NotEnough("--side is for a plan given as flags; a written plan carries its "
                                "own side")
            path = Path(args.plan) if args.plan else show.shared("plan.json")
            if not path.exists():
                print(f"No {path}. Run /nse-exit first, or give the plan as flags: "
                      f"--symbol --entry --stop [--target 2R].")
                return 2
            try:
                plan = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as err:
                raise NotEnough(f"{path} is not a readable plan ({err}): make a new one with "
                                f"/nse-exit, or give the plan as flags")
            if not isinstance(plan, dict):
                raise NotEnough(f"{path} is not a plan: it holds a {type(plan).__name__}, not "
                                f"the object /nse-exit writes")
            if not plan.get("date"):
                # As the US pack refuses it: a written plan with no day would be replayed as
                # today's, which may not be the day it was made for.
                raise NotEnough(f"{path} has no date: the plan needs the day it is for, like "
                                f"2026-10-22. Make a new one with /nse-exit")
            if args.or_minutes is not None:
                plan["or_minutes"] = args.or_minutes
                plan.pop("opening_range", None)
            if args.stop_rule != "auto":
                plan["stop_rule"] = args.stop_rule
        if args.window < 0:
            raise NotEnough("--window cannot be negative")
        try:
            found = replay(plan, args.runs, args.seed, args.until, args.offline, args.window, at)
        except (OSError, EOFError, ValueError) as err:
            # A saved chart that is missing, not gzip, or not JSON, or a feed answer that is not
            # JSON: said, never a traceback.
            where = f"the saved chart {args.offline}" if args.offline else "the feed's answer"
            raise NotEnough(f"could not read {where} ({err})")
    except (NotEnough, bars.NoBars) as err:
        print(f"Cannot replay: {err}")
        return 2

    if args.mode == "chart":
        print("\n".join(fan_chart(found)))
        print("\n".join([found["verdict"] + "."] + list(found.get("verdict_notes") or [])))
    elif args.json:
        # The 160 sample paths go to the file, not the screen, as the US pack slims them.
        print(json.dumps({k: v for k, v in found.items() if k != "sample_paths"}, indent=2,
                         default=str))
    else:
        for line in report(found):
            print(line)
    if args.write is not None or args.out:
        try:
            where = show.hand_off("timemachine.json", found, args.out or args.write or None)
        except OSError as err:
            # A folder where the file should go, or no permission: one line, never a traceback.
            print(f"\nCannot write the result: {err}")
            return 2
        if not args.json:
            print(f"\nWrote {where}.")
    if args.mode == "run" and not args.json:
        print("Next: /nse-budget sizes it to your budget and your max loss per trade.")
    return 0


if __name__ == "__main__":
    show.run(main)
