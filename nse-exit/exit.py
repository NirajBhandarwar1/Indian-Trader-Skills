#!/usr/bin/env python3
"""Exit Planner for NSE stocks. Sets the stop and the target before the entry.

Reads the trigger from /nse-entry (or a price you give), puts the stop at the opening-range
midpoint by default, rounds it AWAY from the entry to a tick the exchange accepts, and sets the
target at 2R. Refuses a stop at or above the entry, a target at or below it, or a stop too tight to
survive the spread, and says why.

    exit.py plan                                    # from nse/entry.json: or-mid stop, 2R target
    exit.py plan --stop or-mid --target 1R --write
    exit.py plan --symbol SBIN --entry 961.85 --or-high 959.30 --or-low 950.05
    exit.py plan --stop atr:1.5 --offline SBIN-5m.json   # ATR needs the session's bars
    exit.py plan --symbol SBIN --entry 961.85 --setup level --stop pct:1
    exit.py show                                    # the current nse/plan.json

A stop is a plan, not a guarantee: a gap or a circuit can fill past it.
Exit codes: 0 = a plan (or, for show, a plan exists), 2 = refused or an error.

Long side only. Standard library only. It never places an order.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import zlib
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import bars, clock, show                           # noqa: E402
from nsetools import settings as rules_file                      # noqa: E402

FLAT_BY = "15:20"
DEFAULT_TARGET = "2R"
# The same default /nse-risk applies. Without it here a 0.5R plan passed this skill silently and
# was then blocked by /nse-risk, which the US pack avoids by warning at plan time.
MIN_REWARD_RISK = 1.0
MIN_STOP_PERCENT = 1.0
ATR_BARS = 14
SESSION_MINUTES = 375              # 09:15 to 15:30

# A stop must clear the spread or normal wiggle takes you out of a trade that never went wrong.
# Two limits, and the wider one wins. The percentage is the same one a New York tool uses; the tick
# floor is the Indian part, because 0.05% of a 39-rupee stock is under two paise, which is less
# than a single tick and would be a stop at the entry.
MIN_RISK_PCT = 0.0005
MIN_RISK_TICKS = 2

STOP_ALIASES = {"or-mid": "or-mid", "mid": "or-mid", "or-midpoint": "or-mid",
                "or-low": "or-low", "low": "or-low"}
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
SETUPS = {"orb": "opening-range breakout", "level": "close above a level"}
SIZES = {"1m": "1-minute", "5m": "5-minute", "15m": "15-minute"}
# What a plan says when it is made for another day. `show` recomputes this against its own clock,
# so a saved copy of it is never reprinted.
NOT_TODAY = "this plan is for "
NOTE = "A stop is an order, not a promise: a gap or a circuit can fill it past the price."

PAISA = Decimal("0.01")


class Refused(Exception):
    """The plan cannot be made as asked. Always with the reason and never quietly loosened."""


# ---------------------------------------------------------------- money math, exact, in Decimal
#
# The arithmetic is done in Decimal, as the US pack does it, never in binary floating point.
# Measured with floats: an entry of 961.845 rounded to 961.84 (the float is 961.84499...), and a
# reward:risk that is exactly 1.235 came out 1.23. The plan file still carries plain numbers.

def _number(text, what: str) -> float:
    """A finite number from a flag, a file or the config, or a refusal naming what it was."""
    return float(_decimal(text, what))


def _decimal(text, what: str) -> Decimal:
    """The same, as an exact Decimal: a float by its shortest repr, a text by its own digits."""
    if isinstance(text, bool) or text is None or isinstance(text, (list, dict)):
        raise Refused(f"{what} wants a number, got {text!r}")
    if isinstance(text, float):
        if not math.isfinite(text):
            raise Refused(f"{what} wants a number, got {text!r}")
        return Decimal(repr(text))
    clean = str(text).replace(",", "").replace("Rs.", "").replace("Rs", "").replace("₹", "")
    try:
        value = Decimal(clean.strip())
    except (InvalidOperation, ValueError):
        raise Refused(f"{what} wants a number, got {text!r}")
    if not value.is_finite():
        raise Refused(f"{what} wants a number, got {text!r}")
    return _sized(value, what, text)


# A trillion rupees is no share price, R multiple or setting. Measured: an entry of 1e308 in a
# saved trigger crashed the paisa rounding with a decimal InvalidOperation traceback.
LARGEST = 12


def _sized(value: Decimal, what: str, text) -> Decimal:
    if value and value.adjusted() >= LARGEST:
        raise Refused(f"{what} is too large to be a price or a rule, got {text!r}")
    return value


def _param(text: str, what: str) -> Decimal:
    """The X of price:X, pct:X or atr:K: plain digits, as the US pack reads them.

    1,000.05 and Rs 955 are read; 1e3, nan, inf and 0x10 are not numbers a person writes for a
    price, and are refused rather than guessed at.
    """
    clean = re.sub(r"^(rs\.?|₹)\s*", "", str(text).strip(), flags=re.IGNORECASE)
    if not re.fullmatch(r"-?(\d[\d,]*\.?\d*|\.\d+)", clean):
        raise Refused(f"{what} wants a number, got {text.strip()!r}")
    return _sized(Decimal(clean.replace(",", "")), what, text.strip())


def _paise(value: Decimal) -> Decimal:
    """Half up to the paisa, as the US pack rounds an entry or a range to the cent."""
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


def _num(value: Decimal) -> str:
    """1, 1.5, 952.2315: the exact number, without trailing zeros."""
    text = format(Decimal(value).normalize(), "f")
    return text if text not in ("-0",) else "0"


def _plain(value) -> str:
    """The exact amount with thousands commas and at least two decimals: 1,000.05, 954.675."""
    text = _num(Decimal(value) if not isinstance(value, float) else Decimal(repr(value)))
    sign = "-" if text.startswith("-") else ""
    whole, _, frac = text.lstrip("-").partition(".")
    frac = (frac + "00")[:max(2, len(frac))]
    return f"{sign}{int(whole):,}.{frac}"


def _rs(value) -> str:
    """Rs and the exact amount, so a working figure is shown as it is and never rounded."""
    return f"Rs {_plain(value)}"


def _tick(price: Decimal) -> Decimal:
    return Decimal(str(clock.tick_size(float(price))))


def _to_tick(price: Decimal, up: bool) -> Decimal:
    """A price on a tick NSE accepts: a stop DOWN, a target UP, a price on a tick stays put."""
    tick = _tick(price)
    steps = (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return (steps * tick).quantize(PAISA)


def _ratio(value: Decimal) -> str:
    """A reward:risk for people: 2.0, 1.5, 1.37 (as the US pack prints it)."""
    text = f"{value:.2f}"
    return text[:-1] if text.endswith("0") else text


def _moved(rounded: Decimal, raw: Decimal) -> bool:
    return rounded != raw


# ---------------------------------------------------------------- times

def hhmm(text, what: str) -> _time:
    """'15:20' or '9:45' to a time on the NSE clock, or a refusal saying why not."""
    found = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text))
    if not found or int(found.group(1)) > 23 or int(found.group(2)) > 59:
        raise Refused(f"{what} wants a time like 15:20, got {text!r}")
    return _time(int(found.group(1)), int(found.group(2)))


def clock_time(text, what: str) -> _time:
    """An entry time: 10:15, or 10:15 AM / 2:05 PM as the US pack also takes it."""
    found = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*", str(text))
    if not found:
        raise Refused(f"{what} wants a time like 10:15 (IST), got {text!r}")
    hour, minute = int(found.group(1)), int(found.group(2))
    if found.group(3):
        if not 1 <= hour <= 12:
            raise Refused(f"{what} wants a time like 10:15 (IST), got {text!r}")
        hour = hour % 12 + (12 if found.group(3).upper() == "PM" else 0)
    if hour > 23 or minute > 59:
        raise Refused(f"{what} wants a time like 10:15 (IST), got {text!r}")
    return _time(hour, minute)


def pin(text: str | None) -> str | None:
    """--now as clock.now reads it. Also takes '10:15' (today), '...T10:15', '... IST', and a
    time with Z or an offset, which is turned into IST, as the US pack takes its --now."""
    if not text:
        return text
    raw = str(text).strip()
    if raw.upper().endswith(" IST"):
        raw = raw[:-4].strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", raw):
        raw = f"{clock.now().date().isoformat()} {raw.zfill(5) if len(raw) <= 5 else raw.zfill(8)}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", raw) or raw.endswith("Z") or \
            re.search(r"[+-]\d{2}:?\d{2}$", raw):
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return text                                          # clock.now says what it wants
        if when.tzinfo is not None:
            when = when.astimezone(clock.IST)
        return when.strftime("%Y-%m-%d %H:%M:%S")
    return raw


def check_flat_by(text: str, explicit: bool):
    """The flat-by time must fall inside the session: after the open and before the 15:30 close.

    The US pack refuses a flat-by at or after the close for the same reason: a square-off time
    after the market has shut is a time at which nothing can be done.
    """
    name = "--flat-by" if explicit else "flat_by in the config"
    when = hhmm(text, name)
    if when >= clock.CLOSE:
        raise Refused(f"{name} {when:%H:%M} is at or after the 15:30 close: nothing can be "
                      f"squared off then")
    if when <= clock.OPEN:
        raise Refused(f"{name} {when:%H:%M} is at or before the 09:15 open")
    return when


def check_day(day) -> str:
    """The session date as YYYY-MM-DD, or a refusal. From a flag or a saved file alike."""
    text = str(day).strip()
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError
        _date.fromisoformat(text)
    except ValueError:
        raise Refused(f"the date wants YYYY-MM-DD, not {day!r}")
    return text


def _minutes_of(window: dict) -> int | None:
    """A saved range's length from its own 'from' and 'to' (09:15 to 09:30 is 15)."""
    try:
        start, end = hhmm(window["from"], "from"), hhmm(window["to"], "to")
    except (KeyError, Refused):
        return None
    length = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    return length if length > 0 else None


def check_window(window, minutes_default: int = 15) -> dict | None:
    """An opening range with a numeric high and low, from the flags or a saved file.

    The high and low are rounded half up to the paisa, as the US pack rounds them to the cent.
    """
    if window is None:
        return None
    if not isinstance(window, dict):
        raise Refused(f"the opening range is not a high and a low: {window!r}")
    for key in ("high", "low"):
        if window.get(key) is None:
            raise Refused(f"the opening range has no {key}: give --or-high and --or-low, or "
                          f"run /nse-entry again")
    minutes = window.get("minutes") or minutes_default
    # nan was read by int() as a crash; a float like 15.5 is not a whole number of minutes.
    if isinstance(minutes, bool) or (isinstance(minutes, float) and
                                     (not math.isfinite(minutes) or minutes != int(minutes))):
        raise Refused(f"the opening range's minutes wants a whole number, got {minutes!r}")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError, OverflowError):
        raise Refused(f"the opening range's minutes wants a whole number, got {minutes!r}")
    if minutes < 1:
        raise Refused(f"the opening range's minutes must be above 0, got {minutes}")
    if minutes > SESSION_MINUTES:
        # Measured: a saved range of 1e308 minutes was written into the plan as it was.
        raise Refused(f"the opening range's minutes is {minutes}, longer than the "
                      f"{SESSION_MINUTES}-minute session from 09:15 to 15:30")
    high = _paise(_decimal(window["high"], "the opening-range high"))
    low = _paise(_decimal(window["low"], "the opening-range low"))
    if not high > low:
        # Refused whatever the stop rule: a plan carrying a range upside down is not a range.
        raise Refused(f"the opening-range high {_rs(high)} must be above its low {_rs(low)}")
    return {"high": float(high), "low": float(low), "minutes": minutes}


CONFIG_PATH: str | None = None     # set from --config


def config() -> dict:
    """Your rules, from --config or the usual place. See nsetools.settings for where it looks."""
    return rules_file.load(CONFIG_PATH)


def _setting(settings: dict, key: str, default: float) -> tuple[Decimal, bool]:
    """A numeric rule from the config and whether it came from there, or a refusal."""
    value = settings.get(key)
    if value is None:
        return Decimal(repr(default)), False
    return _decimal(value, f"{key} in the config"), True


# ---------------------------------------------------------------- bars for an ATR stop

def bars_to_entry(symbol: str, interval: str, day: str | None, offline: str | None,
                  entry_bar: int | None = None, entry_time: str | None = None,
                  at=None) -> list[dict]:
    """The session's bars up to and including the entry bar, and none after it.

    An ATR stop measured on the whole session uses bars that had not happened when the trade was
    entered -- the first version of this file did exactly that, so a stop set at 10:15 was sized on
    volatility from the afternoon. Cut at the entry bar, the entry time, or failing both the
    clock, it can only use what was known at the entry.

    The bar number counts from 1, as /nse-entry writes it, and is the bar's place ON THE CLOCK
    (bar 12 is the 10:10 bar), never its place in the list: with a bar missing from the feed the
    twelfth bar in the list is the 10:15 one, which closed after a 10:15 entry. Bar 0 once read as
    "no bar given" and used the whole session, and a negative number dropped bars from the end.
    """
    if entry_bar is not None and (isinstance(entry_bar, bool) or int(entry_bar) < 1):
        raise Refused(f"the entry bar counts from 1, got {entry_bar}")
    if offline and not Path(offline).exists():
        raise Refused(f"no saved bars at {offline}: name a saved chart, or a folder of them")
    try:
        rows = bars.candles(symbol, interval, day=day, at=at, offline=offline)
    except bars.NoBars:
        raise
    # EOFError and zlib.error are a cut-off or corrupt .gz, OverflowError a time stamp no clock
    # has: each was a traceback before it was listed here.
    except (OSError, EOFError, zlib.error, ValueError, TypeError, AttributeError, KeyError,
            IndexError, OverflowError) as err:
        raise Refused(f"could not read the bars in {offline}: {err}") if offline else \
            Refused(f"the bars could not be read: {err}")
    # The feed's own time of its last trade. A bar that ends after it had not finished when the
    # chart was saved, whatever the clock says now, as the US pack drops it by the chart's
    # regularMarketTime: a chart saved at 11:02 holds an 11:00 bar two minutes old, and an ATR
    # that counted it would be sized on half a bar.
    traded = bars.last_trade(symbol)
    if traded is not None:
        rows = [b for b in rows if b["end"] <= traded]
    # One bar per slot: a feed that sends a bar twice keeps its last copy, as the US pack does.
    by_start = {}
    for row in rows:
        by_start[row["start"]] = row
    rows = [by_start[k] for k in sorted(by_start)]
    if entry_bar is not None:
        step = timedelta(minutes=bars.MINUTES[interval])
        rows = [b for b in rows
                if (b["start"] - b["start"].replace(hour=clock.OPEN.hour, minute=clock.OPEN.minute,
                                                    second=0, microsecond=0)) // step + 1
                <= int(entry_bar)]
    if entry_time:
        rows = [b for b in rows if b["end"].strftime("%H:%M") <= entry_time]
    return rows


# ---------------------------------------------------------------- the stop and the target

def canonical_stop(rule: str | None) -> str:
    rule = (rule or "or-mid").strip().lower().replace("_", "-")
    return STOP_ALIASES.get(rule, rule)


def stop_from(rule: str, entry, window: dict | None, symbol: str,
              day: str | None, interval: str, offline: str | None,
              entry_bar: int | None = None, entry_time: str | None = None,
              at=None) -> dict:
    """The stop, the sentence explaining where it came from, and the ATR when one was used."""
    rule = canonical_stop(rule)
    e = _decimal(entry, "the entry price")
    if rule in ("or-mid", "or-low") and window:
        high, low = _decimal(window["high"], "the opening-range high"), \
            _decimal(window["low"], "the opening-range low")
        if not high > low:
            raise Refused(f"the opening-range high {_rs(high)} must be above its low {_rs(low)}")

    measured = None
    detail = ""
    if rule == "or-mid":
        if not window:
            raise Refused("an opening-range stop needs an opening range (--or-high and --or-low), "
                          "and this setup has none. Name one with --stop price:X or --stop pct:X")
        raw = (high + low) / 2
        label = "opening-range midpoint"
        detail = f"({_rs(high)} + {_rs(low)}) / 2 = {_rs(raw)}"
        why = f"{label}: {detail}"
    elif rule == "or-low":
        if not window:
            raise Refused("an opening-range stop needs an opening range (--or-high and --or-low), "
                          "and this setup has none")
        raw = low
        label = "opening-range low"
        why = f"opening-range low {_rs(raw)}: wider, so fewer shares for the same loss"
    elif rule.startswith("price:"):
        raw = _param(rule.split(":", 1)[1], "--stop price:X")
        if raw <= 0:
            raise Refused("--stop price:X needs a price above 0")
        label, rule = "your price", f"price:{_num(raw)}"     # stored as the number, not the typing
        why = f"your price {_rs(raw)}"
    elif rule.startswith("pct:"):
        pct = _param(rule.split(":", 1)[1], "--stop pct:X")
        if not 0 < pct < 100:
            raise Refused(f"--stop pct:X needs X above 0 and under 100, got {_num(pct)}")
        raw = e * (1 - pct / 100)
        label, rule = f"{_num(pct)}% under the entry", f"pct:{_num(pct)}"
        detail = f"{_rs(e)} x (1 - {_num(pct)}%) = {_rs(raw)}"
        why = f"{label}: {detail}"
    elif rule.startswith("atr:"):
        multiple = _param(rule.split(":", 1)[1], "--stop atr:K")
        if multiple <= 0:
            raise Refused("--stop atr:K needs K above 0")
        rows = bars_to_entry(symbol, interval, day, offline, entry_bar, entry_time, at)
        if len(rows) < 3:
            raise Refused(f"an ATR stop needs at least 3 closed {interval} bars of {symbol} on "
                          f"{day} by the entry; found {len(rows)}")
        measured = bars.atr(rows, ATR_BARS, places=None)
        if not measured:
            raise Refused("there are not enough bars to measure an ATR yet")
        value, used = measured
        # Six places for the arithmetic, as the US pack sets its ATR stop.
        six = Decimal(repr(round(value, 6)))
        raw = e - multiple * six
        label, rule = f"{_num(multiple)} x ATR({used}) under the entry", f"atr:{_num(multiple)}"
        detail = (f"Rs {_plain(e)} - {_num(multiple)} x {_num(six)} = {_rs(raw)}"
                  + ("" if used >= ATR_BARS else
                     f" (only {used} bars had closed, so it averaged those)"))
        why = (f"{_num(multiple)} x the {used}-bar ATR of {_rs(six)} under the entry: {detail}")
    else:
        raise Refused(f"{rule!r} is not a stop rule. Use or-mid, or-low, price:X, pct:X or atr:K")

    stop = _to_tick(raw, up=False) if raw > 0 else raw.quantize(PAISA, rounding=ROUND_FLOOR)
    if _moved(stop, raw):
        why += f", rounded down to the tick {_rs(stop)}"
        detail = (detail + "," if detail else _rs(raw)) + f" rounded down to the tick {_rs(stop)}"
    return {"stop": stop, "why": why, "rule": rule, "label": label, "detail": detail,
            "atr": measured[0] if measured else None, "atr_bars": measured[1] if measured else None}


def target_from(rule: str, entry, risk) -> tuple[Decimal, str, Decimal | None]:
    """The target, its sentence, and the R multiple asked for (None for a price target)."""
    e, risk = _decimal(entry, "the entry price"), _decimal(risk, "the risk")
    rule = (rule or DEFAULT_TARGET).strip().upper()
    if rule.startswith("PRICE:"):
        raw = _param(rule.split(":", 1)[1], "--target price:X")
        if raw <= 0:
            raise Refused("--target price:X needs a price above 0")
        target = _to_tick(raw, up=True)
        why = f"your price {_rs(raw)}"
        if _moved(target, raw):
            why += f", rounded up to the tick {_rs(target)}"
        if risk > 0 and target > e:
            why += f" ({_ratio(((target - e) / risk).quantize(PAISA, rounding=ROUND_HALF_UP))}R)"
        return target, why, None
    match = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)\s*R", rule)
    if match:
        multiple = _sized(Decimal(match.group(1)), "--target", rule)
        if multiple <= 0:
            raise Refused("--target needs an R multiple above 0, like 1R or 2R")
        raw = e + multiple * risk
        target = _to_tick(raw, up=True)
        word = WORDS.get(int(multiple)) if multiple == multiple.to_integral_value() else None
        why = (f"{_num(multiple)}R ({word or _num(multiple)} times the risk): {_rs(e)} + "
               f"{_num(multiple)} x {_rs(risk)} = {_rs(raw)}")
        if _moved(target, raw):
            why += f", rounded up to the tick {_rs(target)}"
        return target, why, multiple
    raise Refused(f"{rule!r} is not a target. Use 2R, 1R, 1.5R or price:X")


def _target_text(rule) -> str:
    """The target rule as the plan stores it: 2R, 1.5R or price:X, as the number and not the
    typing, the way the stop rule is stored (price:Rs 1,000 is price:1000, 2.50r is 2.5R)."""
    text = str(rule or DEFAULT_TARGET).strip()
    if text.lower().startswith("price:"):
        return "price:" + _num(_param(text.split(":", 1)[1], "--target price:X"))
    return _num(Decimal(re.sub(r"\s*[Rr]$", "", text))) + "R"


def _percent(value: Decimal, gate: Decimal) -> str:
    """value% to two places, but never rounded up to look as if it reached the gate."""
    shown = value.quantize(PAISA, rounding=ROUND_HALF_UP)
    if shown >= gate > value:
        shown = value.quantize(Decimal("0.0001"), rounding=ROUND_FLOOR)
        return f"{shown:.4f}" if shown < gate else f"just under {_num(gate)}"
    return f"{shown:.2f}"


def _blocked(e: Decimal, stop: Decimal, gate: Decimal) -> bool:
    """Whether /nse-risk's min_stop_percent check blocks this stop, by its own rule exactly.

    /nse-risk compares in whole paise: blocked when risk_paise * 100 < round(floor *
    entry_paise, 6). So a stop exactly on the gate (965.00 to 955.35 is 1%) passes there and is
    not warned about here; one a paisa inside it (955.40) is.
    """
    floor = float(gate)
    if not floor:
        return False
    risk_paise = int((e - stop) * 100)
    entry_paise = int(e * 100)
    return 0 < risk_paise and risk_paise * 100 < round(floor * entry_paise, 6)


def make(symbol: str, entry, stop_rule: str | None = None,
         target_rule: str = DEFAULT_TARGET, window: dict | None = None, day: str | None = None,
         interval: str = "5m", setup: str = "orb", entry_time: str | None = None,
         flat_by: str | None = None, offline: str | None = None, entry_bar: int | None = None,
         at=None, or_minutes: int = 15) -> dict:
    """The whole plan, or a refusal with its reason."""
    settings = config()
    least, least_set = _setting(settings, "min_reward_risk", MIN_REWARD_RISK)
    gate, gate_set = _setting(settings, "min_stop_percent", MIN_STOP_PERCENT)
    at = at or clock.now()
    explicit = bool(flat_by)
    flat_by = flat_by or settings.get("flat_by") or FLAT_BY
    flat_time = check_flat_by(flat_by, explicit)
    flat_by = f"{flat_time:%H:%M}"
    # A plan ALWAYS carries its day. One made by hand used to carry none, and with no date
    # /nse-budget's "this plan is for another day" check had nothing to compare and let a stale
    # plan through. With no date given it is today's, the same as the US pack.
    day = check_day(day) if day else at.date().isoformat()
    if not isinstance(interval, str) or interval not in bars.MINUTES:
        raise Refused(f"the interval is one of {', '.join(bars.MINUTES)}, not {interval!r}")
    window = check_window(window, or_minutes)
    if entry_bar is not None:
        if isinstance(entry_bar, bool) or not isinstance(entry_bar, int):
            raise Refused(f"the entry bar wants a whole number, got {entry_bar!r}")
        if entry_bar < 1:
            raise Refused(f"the entry bar counts from 1 (the 09:15 bar is bar 1), got {entry_bar}")
    setup = str(setup or "orb")

    if not stop_rule:
        if setup != "orb":
            raise Refused(f"a {setup} setup has no opening range to put a stop under: give "
                          f"--stop price:X, pct:X or atr:K")
        stop_rule = "or-mid"

    typed = _decimal(entry, "the entry price")
    e = _paise(typed)
    if e <= 0:
        raise Refused("the entry price must be above 0")
    if entry_time:
        entry_at = clock_time(entry_time, "the entry time")
        entry_time = f"{entry_at:%H:%M}"
        if entry_at < clock.OPEN:
            raise Refused(f"the entry at {entry_time} is before the 09:15 open")
        if entry_at >= flat_time:
            raise Refused(f"the entry at {entry_time} is at or after the flat-by time {flat_by}, "
                          f"so there is no trade left in the day")
    if entry_bar is not None:
        # Bar n closes at 09:15 + n bars. A plan that disagrees with itself would be replayed
        # from one bar and sized from another; /nse-timemachine refuses it too.
        step = bars.MINUTES[interval]
        closes = clock.OPEN.hour * 60 + clock.OPEN.minute + step * entry_bar
        closes_at = f"{closes // 60:02d}:{closes % 60:02d}"
        when = f"at {closes_at}" if closes < 24 * 60 else "after midnight, past the session"
        # The same rule as an entry time: given a bar alone, bar 75 of a 5-minute day (it closes
        # 15:30) was planned as an entry with no time left to hold it.
        if not entry_time and closes >= flat_time.hour * 60 + flat_time.minute:
            raise Refused(f"the entry bar {entry_bar} closes {when} on {interval} bars, at or "
                          f"after the flat-by time {flat_by}, so there is no trade left in the day")
    if entry_bar is not None and entry_time:
        if closes_at != entry_time:
            raise Refused(f"the entry bar {entry_bar} closes {when} on {interval} bars, "
                          f"but the entry time is {entry_time}: give one, or the two that agree")

    found = stop_from(stop_rule, e, window, symbol, day, interval, offline,
                      entry_bar, entry_time, at)
    stop, stop_why, stop_rule = found["stop"], found["why"], found["rule"]
    if stop >= e:
        raise Refused(f"the stop {_rs(stop)} ({found['label']}) is at or above the entry "
                      f"{_rs(e)}, which is not a stop for a long: a long's stop goes under "
                      f"the entry")
    if stop <= 0:
        raise Refused(f"the stop works out to {_rs(stop)}: a stop has to be a price above 0")

    risk = e - stop
    by_percent = e * Decimal(repr(MIN_RISK_PCT))
    by_ticks = MIN_RISK_TICKS * _tick(e)
    floor = max(by_percent, by_ticks)
    if risk < floor:
        raise Refused(f"the risk of {_rs(risk)} a share is under {_rs(floor)}, the wider of 0.05% "
                      f"of the price ({_rs(by_percent)}) and two ticks ({_rs(by_ticks)}): too "
                      f"tight to survive the spread, normal wiggle would stop you out of a trade "
                      f"that never went wrong. Use a wider stop")

    target, target_why, asked_r = target_from(target_rule, e, risk)
    if target <= e:
        raise Refused(f"the target {_rs(target)} is at or below the entry {_rs(e)}: a long's "
                      f"target goes above the entry")

    # /nse-risk is the gate, and it refuses a stop closer than min_stop_percent because on an
    # Indian mid cap that band is inside one minute's range. This does not duplicate that check --
    # two skills enforcing one rule is how they drift apart -- but a plan that is certain to be
    # blocked should say so here, not three steps later. Measured: the opening-range midpoint on
    # SBIN came out 0.75% away, under the 1% default.
    risk_percent = risk / e * 100
    whose = "your" if gate_set else "the default"
    warning = None
    if _blocked(e, stop, gate):
        how_far = f"{_percent(risk_percent, gate)}% from the entry, inside"
        # Only a stop that was worked out and checked against the same gate is offered. "--stop
        # or-low would pass" was said of plans with no range, and of plans already on or-low.
        better = []
        if window and stop_rule != "or-low":
            better.append("or-low")
        for pct in (gate,):
            if 0 < pct < 100:
                better.append(f"pct:{_num(pct)}")
        passing = []
        for rule in better:
            try:
                other = stop_from(rule, e, window, symbol, day, interval, offline, entry_bar,
                                  entry_time, at)["stop"]
            except Refused:
                continue
            if 0 < other < e and e - other >= floor and not _blocked(e, other, gate) and \
                    not (rule.startswith("pct:") and any(p.startswith("pct:") for p in passing)):
                passing.append(rule)
        fix = (f" A wider stop ({', or '.join('--stop ' + p for p in passing)}) would pass"
               if passing else " A wider stop would pass" if gate < 100 else
               " No stop above 0 is that far under an entry")
        warning = (f"this stop is {how_far} {whose} min_stop_percent of {_num(gate)}%, so "
                   f"/nse-risk will block it.{fix}")

    reward = target - e
    reward_risk = (reward / risk).quantize(PAISA, rounding=ROUND_HALF_UP)
    warnings = [warning] if warning else []
    # Against the exact ratio, as /nse-budget refuses it, not only the one rounded for showing:
    # 9.95 on a 10.00 risk is 0.995, shown as 1.0, and passed here silently while /nse-budget
    # refused it. Shown so, it is never rounded up to look as if it reached the minimum.
    if reward / risk < least:
        shown = _ratio(reward_risk) if reward_risk < least else _percent(reward / risk, least)
        warnings.append(f"reward:risk {shown} is under "
                        f"{'your' if least_set else 'the default'} min_reward_risk "
                        f"{_num(least)}; /nse-budget and /nse-risk check that")
    if _moved(_to_tick(e, up=False), e):
        warnings.append(f"the entry {_rs(e)} is not on the Rs {_tick(e):.2f} tick NSE accepts "
                        f"at this price: an order fills at {_rs(_to_tick(e, up=False))} "
                        f"or {_rs(_to_tick(e, up=True))}, so the risk a share is "
                        f"not exactly {_rs(risk)}")
    if e != typed:
        warnings.append(f"the entry {_rs(typed)} was rounded half up to the paisa, {_rs(e)}: "
                        f"every figure here is worked from {_rs(e)}")
    if day != at.date().isoformat():
        warnings.append(f"{NOT_TODAY}{day}, not today ({at.date().isoformat()})")
    plan = {
        "created_by": "nse-exit",
        "created_at": at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "warning": warning, "warnings": warnings,
        "symbol": str(symbol).upper(), "side": "buy", "setup": setup, "date": day,
        "entry": float(e), "entry_time": entry_time, "entry_bar": entry_bar,
        "opening_range": ({"high": window["high"], "low": window["low"],
                           "minutes": window["minutes"]} if window else None),
        "stop": float(stop), "stop_rule": stop_rule, "stop_why": stop_why, "stop_label": stop_why,
        "stop_detail": found["detail"],
        "target": float(target), "target_rule": _target_text(target_rule),
        "target_why": target_why, "target_label": target_why,
        # target_r is the multiple ASKED for (2R stays 2.0), as the US pack stores it; the R the
        # tick-rounded target actually gives is target_r_actual. A price target has no multiple
        # asked for, so both are the actual one.
        "target_r": float(asked_r if asked_r is not None else reward_risk),
        "target_r_actual": float(reward_risk),
        "risk_per_share": float(risk), "reward_per_share": float(reward),
        "risk_percent": float(risk_percent.quantize(PAISA, rounding=ROUND_HALF_UP)),
        "reward_risk": float(reward_risk),
        "flat_by": flat_by, "interval": interval, "currency": "INR",
        "note": NOTE,
    }
    if found["atr"] is not None:
        plan["atr"] = round(found["atr"], 4)
        plan["atr_bars"] = found["atr_bars"]
    return plan


# ---------------------------------------------------------------- files

def read_saved(path: Path, what: str) -> dict:
    """A saved JSON object, or a plain refusal: missing, unreadable, not JSON, or not an object."""
    if not path.exists():
        raise Refused(f"no {what} at {path}")
    if path.is_dir():
        raise Refused(f"{path} is a folder, not a {what.split(' (')[0]} file")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        raise Refused(f"{path} is not valid JSON ({err})")
    if not isinstance(saved, dict):
        raise Refused(f"{path} does not hold a {what.split(' (')[0]}: it is a "
                      f"{type(saved).__name__}")
    return saved


def _shared(name: str) -> Path:
    """nse/NAME for reading. show.shared() makes the folder, and with a FILE named nse in the way
    that was a FileExistsError traceback; reading needs no folder made, so it is not made here."""
    try:
        return show.shared(name)
    except OSError:
        return Path.cwd() / "nse" / name


def from_entry_file(offline: str | None = None, where: str | None = None,
                    planned_ahead: bool = False) -> tuple[dict, Path]:
    path = Path(where) if where else _shared("entry.json")
    saved = read_saved(path, "trigger (run /nse-entry first, or give --symbol and --entry)")
    if str(saved.get("side") or "buy").lower() not in ("buy", "long"):
        raise Refused(f"{path} is a {saved.get('side')}: this plans long trades only")
    if saved.get("status") != "triggered" and not planned_ahead:
        raise Refused(f"the saved setup for {saved.get('symbol')} is "
                      f"'{saved.get('status') or 'unknown'}', not triggered, so there is no "
                      f"entry to plan around. Plan once it triggers, or pass --entry PRICE to "
                      f"plan ahead")
    return saved, path


def check_plan(plan, path: Path) -> dict:
    """A saved plan `show` can print, or a refusal saying what is wrong with it."""
    if not isinstance(plan, dict):
        raise Refused(f"{path} does not hold a plan: it is a {type(plan).__name__}")
    missing = [k for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share",
                           "reward_risk", "flat_by") if plan.get(k) is None]
    if missing:
        raise Refused(f"{path} is missing {', '.join(missing)}: make a new plan with "
                      f"exit.py plan --write")
    for key in ("entry", "stop", "target", "risk_per_share", "reward_risk"):
        if isinstance(plan[key], bool) or not isinstance(plan[key], (int, float)) \
                or not math.isfinite(plan[key]):
            raise Refused(f"{path} has {key} {plan[key]!r}, which is not a number: make a new "
                          f"plan with exit.py plan --write")
    if plan["entry"] <= 0:
        raise Refused(f"{path} has an entry of {plan['entry']}, which is not a price: make a new "
                      f"plan with exit.py plan --write")
    # A plan that contradicts itself is not shown as a plan: each of these prints a stop or a
    # risk that is not the trade's.
    if not 0 < plan["stop"] < plan["entry"]:
        raise Refused(f"{path} has a stop of {plan['stop']} against an entry of {plan['entry']}: "
                      f"a long's stop goes under the entry. Make a new plan with exit.py plan "
                      f"--write")
    if not plan["target"] > plan["entry"]:
        raise Refused(f"{path} has a target of {plan['target']} against an entry of "
                      f"{plan['entry']}: a long's target goes above it. Make a new plan with "
                      f"exit.py plan --write")
    if abs(plan["entry"] - plan["stop"] - plan["risk_per_share"]) > 0.005:
        raise Refused(f"{path} has a risk a share of {plan['risk_per_share']}, but its entry less "
                      f"its stop is {plan['entry'] - plan['stop']:.2f}: make a new plan with "
                      f"exit.py plan --write")
    # The reward:risk printed is the plan's own, worked out again from its entry, stop and target
    # exactly as `plan` works it out. Measured: a plan saying 5.0 on a 2R trade printed 5.0.
    e, s, t = (Decimal(repr(float(plan[k]))) for k in ("entry", "stop", "target"))
    try:
        worked = ((t - e) / (e - s)).quantize(PAISA, rounding=ROUND_HALF_UP)
        said = Decimal(repr(float(plan["reward_risk"]))).quantize(PAISA, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise Refused(f"{path} holds numbers too large to be a plan: make a new plan with "
                      f"exit.py plan --write")
    if said != worked:
        raise Refused(f"{path} has a reward:risk of {plan['reward_risk']}, but its own entry, stop "
                      f"and target give {_ratio(worked)}: make a new plan with exit.py plan "
                      f"--write")
    plan["date"] = check_day(plan["date"])
    texts = ("setup", "interval", "entry_time", "symbol", "stop_why", "stop_label", "target_why",
             "target_label")
    for key in texts:
        if plan.get(key) is not None and not isinstance(plan[key], str):
            raise Refused(f"{path} has {key} {plan[key]!r}, which is not text: make a new plan "
                          f"with exit.py plan --write")
    if not plan["symbol"].strip():
        raise Refused(f"{path} has no symbol: make a new plan with exit.py plan --write")
    # Each of these was printed as it was: "on abc bars", "at abc (bar 0)", "abc IST".
    if plan.get("interval") is not None and plan["interval"] not in bars.MINUTES:
        raise Refused(f"{path} has interval {plan['interval']!r}, not one of "
                      f"{', '.join(bars.MINUTES)}: make a new plan with exit.py plan --write")
    if plan.get("entry_time") is not None:
        clock_time(plan["entry_time"], f"{path}'s entry_time")
    bar = plan.get("entry_bar")
    if bar is not None and (isinstance(bar, bool) or not isinstance(bar, int) or bar < 1):
        raise Refused(f"{path} has entry_bar {bar!r}, which is not a bar number from 1: make a "
                      f"new plan with exit.py plan --write")
    if bar is not None and plan.get("entry_time") is not None:
        # Printed together as "at 10:15 (bar 12)", so they must be the same moment, as `plan`
        # requires: a saved bar of 10**30 beside 10:15 was printed as it was.
        step = bars.MINUTES[plan.get("interval") or "5m"]
        closes = clock.OPEN.hour * 60 + clock.OPEN.minute + step * bar
        said = clock_time(plan["entry_time"], f"{path}'s entry_time")
        if closes != said.hour * 60 + said.minute:
            raise Refused(f"{path} has entry bar {bar} and entry time {plan['entry_time']}, which "
                          f"are not the same moment on {plan.get('interval') or '5m'} bars: make "
                          f"a new plan with exit.py plan --write")
    if not isinstance(plan["flat_by"], str):
        raise Refused(f"{path} has flat_by {plan['flat_by']!r}, which is not a time like 15:20")
    hhmm(plan["flat_by"], f"{path}'s flat_by")
    return plan


# ---------------------------------------------------------------- the command line

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode")
    p = sub.add_parser("plan", help="set the stop and the target")
    p.add_argument("--from", dest="from_file", metavar="PATH",
                   help="the trigger to plan around (default nse/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=show.number,
                   help="the entry price, or a plan-ahead price with --from")
    p.add_argument("--or-high", type=show.number)
    p.add_argument("--or-low", type=show.number)
    p.add_argument("--or-minutes", type=int, default=None,
                   help="the opening range in minutes (the trigger's own, else 15)")
    p.add_argument("--setup", choices=("orb", "level"))
    p.add_argument("--date", help="the session date, YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="the entry time, HH:MM IST (10:15 AM also works)")
    p.add_argument("--entry-bar", type=int, help="the entry's bar number, from 1, for an ATR stop")
    p.add_argument("--interval", choices=tuple(bars.MINUTES))
    p.add_argument("--side", default="buy", choices=("buy", "long", "sell", "short"),
                   help="long only")
    p.add_argument("--stop", help="or-mid (the orb default) | or-low | price:X | pct:X | atr:K")
    p.add_argument("--target", default=DEFAULT_TARGET, help="2R | 1R | 1.5R | price:X")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:20)")
    p.add_argument("--config", help="a config file instead of the usual places")
    p.add_argument("--offline", help="a saved chart JSON, or a folder of them, for an ATR stop")
    p.add_argument("--now", help="pin the clock, like '2026-10-22 10:15' (IST)")
    p.add_argument("--write", nargs="?", const=True, metavar="PATH",
                   help="save the plan, to nse/plan.json or to PATH")
    p.add_argument("--out", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="print the current plan")
    s.add_argument("--plan", help="the plan to print (default nse/plan.json)")
    s.add_argument("--now", help="pin the clock, like '2026-10-22 10:15' (IST)")
    s.add_argument("--json", action="store_true")
    return ap


def main(argv=None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.mode:
        ap.print_help()
        return 2

    global CONFIG_PATH
    try:
        at = clock.now(pin(args.now))
        at.astimezone(timezone.utc)
    except clock.BadTime as err:
        print(f"Refused: {err}")
        return 2
    except OverflowError:
        # Measured: --now '0001-01-01 00:00' is a time in IST with no UTC to record it in.
        print(f"Refused: --now {args.now!r} is outside the calendar this can work in")
        return 2

    if args.mode == "show":
        return _show(args, at)

    CONFIG_PATH = args.config
    if args.config and Path(args.config).is_dir():
        # It read "is not valid JSON ([Errno 13] Permission denied ...)", which is not the fault.
        print(f"Refused: --config {args.config} is a folder, not a config file.")
        return 2
    try:
        config()
    except FileNotFoundError as err:
        print(f"Refused: {err}. A config named with --config has to exist.")
        return 2
    except rules_file.BadConfig as err:
        print(f"Refused: {err}")
        return 2

    if args.side in ("sell", "short"):
        print("Refused: this plans long trades only, so there is no stop above a short entry. "
              "Plan a buy instead.")
        return 2
    if args.symbol and args.from_file:
        print("Refused: use --from or --symbol, not both.")
        return 2
    if (args.or_high is None) != (args.or_low is None):
        print("Refused: give both --or-high and --or-low, or neither.")
        return 2
    or_minutes = args.or_minutes if args.or_minutes is not None else 15

    window = None
    if args.or_high is not None:
        window = {"high": args.or_high, "low": args.or_low, "minutes": or_minutes}

    try:
        if args.symbol:
            if args.entry is None:
                print("Refused: --symbol needs --entry PRICE.")
                return 2
            symbol, entry = args.symbol.strip().upper(), args.entry
            if not symbol:
                raise Refused("--symbol needs a symbol")
            setup = args.setup or ("orb" if window else "level")
            when, entry_time, entry_bar = args.date, args.time, args.entry_bar
            interval = args.interval or "5m"
        else:
            planned_ahead = args.entry is not None
            saved, where = from_entry_file(args.offline, args.from_file, planned_ahead)
            # Each text field is text or absent. Measured: a symbol of true was planned as
            # "TRUE", and a trigger_time of false or 0 was written into the plan as the entry
            # time for /nse-timemachine to read.
            for key in ("symbol", "setup", "date", "interval", "trigger_time", "time"):
                if saved.get(key) is not None and not isinstance(saved[key], str):
                    raise Refused(f"{where} has {key} {saved[key]!r}, which is not text: run "
                                  f"/nse-entry again")
            symbol = saved.get("symbol")
            # /nse-entry writes entry and trigger_time, and also price and time, the names the
            # US pack reads; either pair is read, the first when both are there.
            saved_entry = saved.get("entry") if saved.get("entry") is not None \
                else saved.get("price")
            saved_time = saved.get("trigger_time") or saved.get("time") or None
            entry = args.entry if planned_ahead else saved_entry
            setup = args.setup or saved.get("setup") or "orb"
            when = args.date or saved.get("date")
            entry_time = args.time or (None if planned_ahead else saved_time)
            entry_bar = (args.entry_bar if args.entry_bar is not None
                         else (None if planned_ahead else saved.get("bar_index")))
            if entry_bar is not None and (isinstance(entry_bar, bool)
                                          or not isinstance(entry_bar, int)):
                raise Refused(f"{where} has bar_index {entry_bar!r}, which is not a bar number")
            interval = args.interval or saved.get("interval") or "5m"
            if window is None and saved.get("opening_range") is not None:
                window = saved["opening_range"]
                if isinstance(window, dict):
                    # The range's own length, as the trigger saved it. A 30-minute trigger
                    # planned as a 15-minute one would be replayed on the wrong range.
                    minutes = (window.get("minutes") or saved.get("or_minutes")
                               or _minutes_of(window) or or_minutes)
                    window = dict(window, minutes=minutes)
            symbol = (symbol or "").strip().upper()
            if not symbol or entry is None:
                raise Refused(f"{where} has no symbol or entry price")

        plan = make(symbol, entry, args.stop, args.target, window, when, interval,
                    setup, entry_time, args.flat_by, args.offline, entry_bar, at,
                    or_minutes)
    except Refused as err:
        print(f"Refused: {err}")
        return 2
    except bars.NoBars as err:
        print(f"Refused: {err}")
        return 2
    except InvalidOperation:
        # The last line of defence: every number is size-checked where it is read.
        print("Refused: a number in this plan is too large to work with exactly.")
        return 2

    where = args.out or (args.write if isinstance(args.write, str) else None)
    written = None
    if args.write or args.out:
        if where and Path(where).is_dir():
            print(f"Refused: {where} is a folder: --write takes the plan file's own path, like "
                  f"{Path(where) / 'plan.json'}. Nothing was written.")
            return 2
        try:
            written = show.hand_off("plan.json", plan, where)
        except OSError as err:
            # A traceback before: the plan is not saved, and saying so is the whole message.
            print(f"Refused: the plan could not be written to {where or 'nse/plan.json'} "
                  f"({err.strerror or err}). Nothing was written.")
            return 2
    _report(plan, args.json)
    if written:
        # Never on stdout with --json: one more line there and the output is not JSON.
        print(f"\nwrote {written}", file=sys.stderr if args.json else sys.stdout)
    return 0


def _show(args, at) -> int:
    path = Path(args.plan) if args.plan else _shared("plan.json")
    if not path.exists():
        print(f"No {path} yet. Make one: exit.py plan --write.")
        return 2
    try:
        plan = check_plan(read_saved(path, "plan"), path)
    except Refused as err:
        print(f"Refused: {err}")
        return 2
    # A field saved as null is a field not saved: "None on None bars" was printed for one.
    plan["stop_why"] = plan.get("stop_why") or plan.get("stop_label") or ""
    plan["target_why"] = plan.get("target_why") or plan.get("target_label") or ""
    plan["interval"] = plan.get("interval") or "5m"
    plan["setup"] = plan.get("setup") or "orb"
    # Worked out from the plan's own entry and risk, never trusted from the file: a saved
    # figure that disagrees with them (or is not a number) would print a risk that is not this one.
    plan["risk_percent"] = float(
        (Decimal(repr(float(plan["risk_per_share"]))) / Decimal(repr(float(plan["entry"]))) * 100)
        .quantize(PAISA, rounding=ROUND_HALF_UP))
    # "Not today" is about the clock NOW. The line saved when the plan was made was true
    # then and may be false now, so it is dropped and worked out again. Only text is a warning:
    # a saved true or 1.5 printed as "WARNING: True.".
    saved = plan.get("warnings") if isinstance(plan.get("warnings"), list) else []
    if not saved and isinstance(plan.get("warning"), str) and plan["warning"]:
        saved = [plan["warning"]]
    plan["warnings"] = [w for w in saved if isinstance(w, str) and w
                        and not w.startswith(NOT_TODAY)]
    if str(plan["date"]) != at.date().isoformat():
        plan["warnings"].append(f"{NOT_TODAY}{plan['date']}, not today "
                                f"({at.date().isoformat()})")
    _report(plan, args.json)
    return 0


def _report(plan: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(plan, indent=2, default=str))
        return
    try:
        when = f", {_date.fromisoformat(str(plan['date'])):%a %Y-%m-%d}"
    except (KeyError, ValueError):
        when = ""
    setup = SETUPS.get(str(plan.get("setup")), str(plan.get("setup")))
    size = SIZES.get(str(plan.get("interval")), str(plan.get("interval")))
    print(f"Exit plan: {plan['symbol']} long{when}, {setup} on {size} bars")
    at = f" at {plan['entry_time']}" if plan.get("entry_time") else ""
    if at and isinstance(plan.get("entry_bar"), int) and not isinstance(plan["entry_bar"], bool):
        at += f" (bar {plan['entry_bar']})"
    print(f"  Entry         Rs {plan['entry']:>10,.2f}{at}")
    print(f"  Stop          Rs {plan['stop']:>10,.2f}  {plan['stop_why']}")
    print(f"  Target        Rs {plan['target']:>10,.2f}  {plan['target_why']}")
    print(f"  Risk a share  Rs {plan['risk_per_share']:>10,.2f} ({plan['risk_percent']:.2f}% of "
          f"the entry)")
    print(f"  Reward:risk      {_ratio(Decimal(repr(float(plan['reward_risk'])))):>10}")
    print(f"  Flat by          {plan['flat_by']:>10} IST, win or lose: no overnight hold")
    for warning in plan.get("warnings") or []:
        print(f"\n  WARNING: {warning}.")
    print("\nSet before the entry. If price hits the stop, you are out; never move it further away."
          "\nA stop is an order, not a promise. A gap at the open or an upper/lower circuit can "
          "fill it\npast your price, or leave it unfilled. Next: /nse-timemachine replays it, then "
          "/nse-budget sizes it.\nNothing was placed.")


if __name__ == "__main__":
    show.run(main)
