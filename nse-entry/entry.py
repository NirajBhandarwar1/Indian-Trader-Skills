#!/usr/bin/env python3
"""Entry Timer for NSE stocks. Watches a setup bar by bar and names the moment to get in.

Reads free keyless 1-minute, 5-minute or 15-minute bars, builds the opening range (NSE opens 09:15,
so the first 15 minutes are the 09:15, 09:20 and 09:25 bars and the range ends 09:30) and calls the
entry on the first completed bar that CLOSES above the range high, or above a level you give it.

    python entry.py check SBIN                         # opening-range breakout, 5-minute bars
    python entry.py check SBIN --now "2026-10-22 10:15" --offline SBIN-5m.json
    python entry.py check SBIN --setup level --level 959.30
    python entry.py check SBIN --interval 1m --min-volume-x 1.5 --write
    python entry.py watch SBIN --every 30 --until 15:00  # one line per closed bar
    python entry.py watchlist                          # every hit in nse/watchlist.json
    python entry.py clock                              # NSE open or closed, next open

A price that pokes above the high in the middle of a bar is not an entry. A bar that CLOSES above
it is, and only once the feed has caught up to that bar's close: on live data the newest closed
bar counts when the feed's last trade is at or after its close, or a minute after its close.

Statuses: triggered; waiting (open, no close above yet); forming (the opening range, or the first
bar, has not closed yet); expired (no trigger by no_entries_after, 15:00 by default); closed (a
holiday or weekend); not_open; no_data.
Exit codes: check 0 = triggered, 1 otherwise, 2 refused. watch 0 on a trigger, 1 otherwise.
watchlist 0 when at least one symbol triggered.

--now takes IST: "2026-10-22 10:15", "2026-10-22T10:15", or just "10:15" for today; a time
ending in Z or an offset such as +00:00 is converted to IST.

Long side only. Nothing here places an order. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time as _time
from datetime import datetime
from datetime import time as clock_time
from datetime import timedelta
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import bars, clock, show                           # noqa: E402
from nsetools import settings as rules_file                      # noqa: E402

# India-specific session rules. The opening range ends at 09:30, so an entry is possible from then
# -- a New York tool waits until 09:45 because its range ends at 09:45. The afternoon cutoff
# leaves room before the intraday square-off: most Indian brokers close an intraday (MIS) position
# themselves around 15:20, which is a broker convention and not an exchange rule, so it is a
# default here and not a fact.
NO_ENTRIES_BEFORE, NO_ENTRIES_AFTER, FLAT_BY = clock_time(9, 30), clock_time(15, 0), "15:20"

# A bar whose close time has passed may still be moving if the feed is behind: its last trade is
# before the bar's close. It counts once the feed catches up, a later bar exists (the feed saying
# this one is finished), or this long has passed since the close, as the US pack counts it.
SETTLE = timedelta(seconds=60)
GRACE = 3              # seconds after a bar closes before a live watch asks for it
MAX_ERRORS = 5         # watch stops after this many failed reads in a row
PAUSE = 0.5            # seconds between symbols in a watchlist
# The bar still forming is named only when the feed's data is not from after the clock: replaying
# a saved day at 10:12 from a file written at the close, "the 10:10 bar so far" would be the
# finished bar, a price nobody knew at 10:12. A few seconds allow for a computer clock running
# behind the exchange's.
SKEW = timedelta(seconds=5)

CONFIG_PATH: str | None = None     # set from --config

# What reading a damaged, truncated or wrong-shaped saved chart raises. Said in one line, as the US
# pack says "could not read", never a traceback: measured before this, a broken .json, a broken
# .gz, a JSON list and a missing file each stopped this skill with a Python traceback.
READ_ERRORS = (OSError, EOFError, ValueError, TypeError, AttributeError, KeyError, IndexError)


def _unreadable(err, offline: str | None) -> str:
    return (f"could not read the bars in {offline}: {err}" if offline
            else f"the bars could not be read: {err}")


class Refused(Exception):
    """A request that cannot be answered as asked, with the reason."""


def config() -> dict:
    """Your rules, from --config or the usual place. See nsetools.settings for where it looks."""
    return rules_file.load(CONFIG_PATH)


def _as_time(raw, fallback):
    try:
        hour, minute = str(raw).split(":")
        return clock_time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


def strict_time(raw, what: str) -> clock_time:
    """An IST wall-clock time like 15:00, or a refusal that says what was wrong.

    A time that cannot be read is never swapped for the default: measured before this, a config
    saying "10:30am" ran silently on 09:30, and --until 1430 silently ran on to 15:00.
    """
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(raw))
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise Refused(f"{what} wants an IST time like 15:00, not {raw!r}")
    return clock_time(int(match.group(1)), int(match.group(2)))


def check_config(settings: dict) -> None:
    where = settings.get("_from") or "the config"
    for key in ("no_entries_before", "no_entries_after"):
        if settings.get(key):
            strict_time(settings[key], f"{key} in {where}")


def pinned_now(text: str | None) -> str | None:
    """--now in the forms the US pack takes, as the "YYYY-MM-DD HH:MM[:SS]" IST the clock reads.

    "10:15" alone is today; "2026-10-22T10:15" is the ISO form; a trailing " IST" is allowed; a
    time ending in Z or an offset (+00:00) is absolute and converted to IST. Anything else is
    passed on unchanged, for the clock to refuse with the form it wants.
    """
    if not text:
        return text
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    alone = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", t)
    if alone:
        t = f"{clock.now():%Y-%m-%d} {int(alone.group(1)):02d}:{alone.group(2)}" + (
            f":{alone.group(3)}" if alone.group(3) else "")
    t = re.sub(r"^(\d{4}-\d{2}-\d{2})T", r"\1 ", t)
    if re.search(r"\d(Z|[+-]\d{2}:?\d{2})$", t) and re.match(r"^\d{4}-\d{2}-\d{2} \d", t):
        try:
            absolute = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return text
        if absolute.tzinfo is not None:
            return absolute.astimezone(clock.IST).strftime("%Y-%m-%d %H:%M:%S")
    return t


def feed_span(day, interval: str, offline: str | None) -> str:
    """The smallest range the free feed must be asked for so that it still holds `day`.

    The feed keeps one-minute bars for about a week and five-minute bars for about 60 days, as the
    US pack measured. Asking for five days on an older --now answered "no closed bars", which read
    as if the stock had not traded. A saved folder is read with the 60-day file when one is there.
    """
    back = (clock.now().date() - day).days
    if back <= 6:
        return "5d"
    if offline:
        return "60d"
    if interval == "1m":
        raise Refused(f"the free feed keeps 1-minute bars for about a week: use --interval 5m "
                      f"for {day}")
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise Refused(f"the free feed keeps intraday bars for about 60 days: {day} is {back} days "
                  f"back, too far to replay without a saved file (--offline)")


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def setup_name(found: dict) -> str:
    """What is being watched, in words: the US pack's head line names the range or the level."""
    if found["setup"] == "orb":
        return f"opening-range breakout ({found['or_minutes']}-minute range)"
    return (f"close above Rs {found['level']:,.2f}" if found.get("level") is not None
            else "close above a level")


def look(symbol: str, setup: str = "orb", level: float | None = None, interval: str = "5m",
         or_minutes: int = 15, min_volume_x: float | None = None, at=None,
         offline: str | None = None) -> dict:
    """What the setup is doing right now: triggered, waiting, forming, expired, closed,
    not_open or no_data.

    Every answer carries a `reason`, the first line of what it says, so nse/entry.json always
    says why, whatever the status.
    """
    found = _look(symbol, setup, level, interval, or_minutes, min_volume_x, at, offline)
    if not found.get("reason"):
        found["reason"] = (found.get("say") or found["status"]).splitlines()[0]
    return found


def _forming_line(found: dict, symbol: str, at, traded_at) -> str | None:
    """The bar still being built, as the US pack names it: never counted, never a trigger.

    Named only when the feed's data is not from after the clock (see SKEW), so a replay never
    shows a finished bar as the price so far.
    """
    live = bars.forming_bar(symbol)
    if not live or traded_at is None or traded_at > at + SKEW:
        return None
    found["forming"] = {"bar_start": f"{live['start']:%H:%M}", "closes": f"{live['end']:%H:%M}",
                        "price": live["close"], "high": live["high"]}
    return (f"The {live['start']:%H:%M} bar is still forming (Rs {live['close']:,.2f} so far, "
            f"Rs {live['high']:,.2f} touched, closes {live['end']:%H:%M}); it does not count "
            f"until it closes.")


def _look(symbol, setup, level, interval, or_minutes, min_volume_x, at, offline) -> dict:
    at = at or clock.now()
    settings = config()
    opens = _as_time(settings.get("no_entries_before"), NO_ENTRIES_BEFORE)
    shuts = _as_time(settings.get("no_entries_after"), NO_ENTRIES_AFTER)
    step = bars.MINUTES[interval]

    # The contract fields are always present, null until known, as the US pack writes them: a
    # reader of nse/entry.json never has to guess whether a missing key means "no trigger".
    found: dict = {"created_by": "nse-entry", "symbol": symbol.upper(), "setup": setup,
                   "side": "buy", "interval": interval, "or_minutes": or_minutes,
                   "as_of": at.isoformat(), "checked_at": at.isoformat(), "status": "no_data",
                   "say": "", "level": level, "flat_by": settings.get("flat_by", FLAT_BY),
                   "date": at.date().isoformat(), "source": "free chart feed (unofficial)",
                   "min_volume_x": min_volume_x,
                   "no_entries_before": f"{opens:%H:%M}", "no_entries_after": f"{shuts:%H:%M}",
                   "time": None, "trigger_time": None, "price": None, "entry": None,
                   "bar_start": None, "bar_index": None, "volume": None, "volume_x": None,
                   "opening_range": None}

    opened = at.replace(hour=clock.OPEN.hour, minute=clock.OPEN.minute, second=0, microsecond=0)
    first_close = opened + timedelta(minutes=step)
    range_ends = opened + timedelta(minutes=or_minutes)
    of = or_minutes // step

    try:
        trading = clock.is_trading_day(at.date(), offline=bool(offline))
    except clock.Unknown as err:
        found["say"] = (f"{err}. Replay a day from a year whose holiday list is known, or run "
                        f"without --offline so the exchange can be asked.")
        found["_failed"] = True
        return found
    if not trading:
        found["status"] = "closed"
        found["say"] = clock.status(at, offline=bool(offline))
        try:
            nxt = clock.next_open(at, offline=bool(offline))
            found["next_open"] = f"{nxt:%Y-%m-%d %H:%M}"
            found["say"] += f"\nNext open: {nxt:%a %Y-%m-%d %H:%M} IST."
        except clock.Unknown:
            pass
        return found
    if at.time() < clock.OPEN:
        found["status"] = "not_open"
        found["say"] = clock.status(at, offline=bool(offline))
        if setup == "orb":
            found["say"] += (f" The {or_minutes}-minute opening range is set at "
                             f"{range_ends:%H:%M}.")
        return found

    try:
        span = feed_span(at.date(), interval, offline)
        rows = bars.candles(symbol, interval, at=at, offline=offline, span=span)
    except Refused as err:
        found["say"], found["_failed"] = f"{err}.", True
        return found
    except bars.NoClosedBars as err:
        # The chart was read and holds no closed bar yet. Before the first bar closes that is
        # expected; inside the opening range it is a range still forming with none of its bars
        # sent yet, as the US pack says. Only after that is it "no data".
        if at >= first_close and not (setup == "orb" and at < range_ends):
            found["say"] = str(err)
            return found
        rows = []
    except bars.NoBars as err:
        found["say"], found["_failed"] = str(err), True
        return found
    except READ_ERRORS as err:
        found["say"], found["_failed"] = _unreadable(err, offline), True
        return found

    # The feed's own last trade. A bar that has closed on the clock but whose close is still ahead
    # of the feed may yet move: it waits, and is said to wait.
    traded_at = bars.last_trade(symbol)
    if traded_at is not None:
        found["data_time"] = traded_at.isoformat()

    if not rows:
        found["status"] = "forming"
        if at < first_close:
            found["say"] = (f"The market is open and no {interval} bar has closed yet; the first "
                            f"closes at {first_close:%H:%M}.")
            if setup == "orb":
                found["say"] += (f" 0 of {of} range bars closed; the range is set at "
                                 f"{range_ends:%H:%M}.")
        else:
            found["say"] = (f"The {or_minutes}-minute opening range is still forming: 0 of {of} "
                            f"range bars closed, the feed has sent none yet. NSE opens 09:15, so "
                            f"it completes at {range_ends:%H:%M}.")
        live = _forming_line(found, symbol, at, traded_at)
        if live:
            found["say"] += "\n" + live
        return found

    held = None
    newest = rows[-1]
    later = bars.newest_start(symbol)
    if traded_at is not None and traded_at < newest["end"] and at < newest["end"] + SETTLE \
            and not (later is not None and later > newest["start"]):
        held = newest
        rows = rows[:-1]
        found["lag_note"] = (f"The feed's last trade is {traded_at:%H:%M:%S}, before the "
                             f"{newest['start']:%H:%M} bar's close: it counts once the feed "
                             f"catches up, a minute at most")
        if not rows:
            found["status"] = "waiting" if newest["start"] >= range_ends or setup != "orb" \
                else "forming"
            found["say"] = found["lag_note"] + "."
            return found

    found["bars"] = len(rows)
    found["_rows"] = rows
    # A range bar held back for lag keeps the range open: built without it, the range would be two
    # bars of three with the third simply late.
    window = (bars.opening_range(rows, or_minutes, at=at)
              if not (held is not None and held["start"] < range_ends) else None)
    if window and window["missing"]:
        gone = ", ".join(window["missing"])
        found["range_note"] = (f"The opening range is built from {window['bars']} of "
                               f"{window['of']} bars: the feed sent no {gone} "
                               f"bar{'s' if len(window['missing']) > 1 else ''}.")
    if window:
        # The day's range, for a level setup too, as the US pack reports and writes it.
        found["opening_range"] = {
            "high": window["high"], "low": window["low"], "minutes": or_minutes,
            "bars": window["bars"], "of": window["of"], "missing": window["missing"],
            "from": window["from"].strftime("%H:%M"), "to": window["to"].strftime("%H:%M"),
            "complete": True,
            "avg_volume": round(window["volume"]) if window["volume"] is not None else None,
            "volume_bars": window["volume_bars"]}
    inside = [b for b in rows if b["start"] < range_ends]
    if window is None and inside and (at < range_ends or held is not None):
        # Said and shown, as the US pack shows it for either setup, but under its own name: a
        # range that is not finished is never handed on as THE opening range, so nse-exit cannot
        # plan a stop on half of one.
        found["opening_range_so_far"] = {
            "high": max(b["high"] for b in inside),
            "low": min(b["low"] for b in inside), "minutes": or_minutes,
            "bars": len(inside), "of": of, "complete": False,
            "from": f"{opened:%H:%M}", "to": f"{range_ends:%H:%M}"}
    if setup == "orb":
        if window is None:
            if at < range_ends or held is not None:
                found["status"] = "forming"
                found["say"] = (f"The {or_minutes}-minute opening range is still forming: "
                                f"{len(inside)} of {of} range bars closed. NSE opens 09:15, so it "
                                f"completes at {range_ends:%H:%M}.")
                if found.get("lag_note"):
                    found["say"] += f"\n{found['lag_note']}."
                else:
                    live = _forming_line(found, symbol, at, traded_at)
                    if live:
                        found["say"] += "\n" + live
            else:
                found["say"] = (f"The feed sent no bars for the opening range "
                                f"({opened:%H:%M}-{range_ends:%H:%M}), so there is no range to "
                                f"break.")
            return found
        beat = window["high"]
        after = [b for b in rows if b["start"] >= window["to"]]
    else:
        if level is None:
            found["say"] = ("a level setup needs a price to beat: pass --level, for example a "
                            "20-day high from /nse-scan")
            return found
        beat = float(level)
        after = rows
    what = "the range high" if setup == "orb" else "the level"
    # The volume a trigger is measured against: the opening range's bars whose volume is KNOWN,
    # for a level setup as for an orb, as the US pack does. It is fixed once the range has closed.
    # Averaging every bar so far instead made a level's ratio move with the clock: measured on
    # SBIN 2026-10-22 at a 1.4x floor, it said waiting at 12:00 and then, at 15:00, reported a
    # trigger at 12:50 that had not been there when 12:50 was checked.
    seen = bars.known_volume([b for b in rows if b["start"] < range_ends])
    reference_volume = (sum(seen) / len(seen)) if seen else None

    found["level"] = round(beat, 2)
    found["_reference_volume"] = reference_volume
    thin, early = None, None

    for bar in after:
        if bar["close"] <= beat:
            continue                                             # a close exactly at it is not above
        if not bars.traded(bar):
            # Open, high, low and close all one price on no volume: nobody traded and the feed
            # carried the last price forward. A close above the level that nobody paid is not an
            # entry. A bar that DID move but reports no volume is different -- it traded, its
            # volume is simply not reported -- and it is not refused here.
            continue
        if bar["end"].time() < opens:
            early = bar                                          # the most recent one, as it is said
            continue                                             # before the day's entry window
        if bar["end"].time() > shuts:
            break                                                # a close AT the cutoff still counts
        ratio = (bar["volume"] / reference_volume
                 if bar["volume"] is not None and reference_volume else None)
        if min_volume_x:
            # Asked for a volume floor, an UNKNOWN volume cannot be said to clear it. It is not
            # treated as passing and it is not treated as zero: the bar is simply not confirmed.
            if ratio is None or ratio < min_volume_x:
                thin = (bar, ratio)
                continue
        minutes_since = int((at - bar["end"]).total_seconds() // 60)
        found.update({
            "status": "triggered",
            "entry": bar["close"],
            "price": bar["close"],
            "trigger_time": bar["end"].strftime("%H:%M"),
            "time": bar["end"].strftime("%H:%M"),
            "bar_start": bar["start"].strftime("%H:%M"),
            # The bar's place ON THE CLOCK from 09:15, counting from 1, as /nse-exit reads it:
            # never its place in the list, which a bar the feed did not send shifts by one.
            "bar_index": int((bar["start"] - opened).total_seconds() // (step * 60)) + 1,
            "volume": bar["volume"],
            "volume_x": round(ratio, 2) if ratio is not None else None,
            "pct_over": round((bar["close"] / beat - 1) * 100, 2),
            "minutes_since": minutes_since,
        })
        volume = (f", {ratio:.2f}x the range's volume"
                  if min_volume_x and ratio is not None else "")
        lines = [f"TRIGGERED {found['trigger_time']} at Rs {bar['close']:,.2f}: the "
                 f"{found['bar_start']} bar (bar {found['bar_index']}) closed above {what} "
                 f"Rs {beat:,.2f} ({found['pct_over']:+.2f}%{volume})."]
        last = rows[-1]
        late = minutes_since > step
        if late:
            lines.append(f"That was {minutes_since} minutes ago; the last close is "
                         f"Rs {last['close']:,.2f} ({last['start']:%H:%M} bar). A late entry is a "
                         f"different trade: the price, the stop distance and the odds have moved.")
        if traded_at is not None and traded_at < bar["end"]:
            found["data_note"] = (f"The feed's last trade is {traded_at:%H:%M:%S}, before this bar "
                                  f"closed: the close can still move a tick or two")
            lines.append(found["data_note"] + ".")
        lines.append("Nothing was placed." if late else
                     "This is the moment to get in, if you take the trade. Nothing was placed.")
        found["say"] = "\n".join(lines)
        found["last"] = {"bar_start": f"{last['start']:%H:%M}", "close": last["close"],
                         "vs_level_pct": round((last["close"] / beat - 1) * 100, 2)}
        found["reason"] = lines[0]
        return found

    last = rows[-1]
    found["last_close"] = last["close"]
    found["last"] = {"bar_start": f"{last['start']:%H:%M}", "close": last["close"],
                     "vs_level_pct": round((last["close"] / beat - 1) * 100, 2)}
    # A bar that closes at or before the cutoff and is only held back for the feed's lag can still
    # trigger: measured on a real file, expiring here said EXPIRED at 15:00:30 and TRIGGERED at
    # 15:01:10, and watch would have stopped on the first.
    pending = held is not None and held["end"].time() <= shuts
    if not pending and at.time() >= shuts:
        found["status"] = "expired"
        found["reason"] = (f"EXPIRED  no close above {what} Rs {beat:,.2f} by {shuts:%H:%M}, the "
                           f"last time for a new entry. Last close Rs {last['close']:,.2f}.")
        found["say"] = found["reason"] + "\nNo entry on this setup today. Nothing was placed."
        return found

    gap = beat - last["close"]
    found["status"] = "waiting"
    if thin and thin[0] is last:
        bar, ratio = thin
        found["say"] = (f"WAITING  the {bar['start']:%H:%M} bar closed above at Rs "
                        f"{bar['close']:,.2f} but on "
                        + (f"{ratio:.2f}x" if ratio is not None else "an unknown")
                        + f" the opening range's average volume, under your {min_volume_x:g}x "
                          f"floor.")
    elif early and early is last:
        found["say"] = (f"WAITING  the {early['start']:%H:%M} bar closed above at Rs "
                        f"{early['close']:,.2f}, but no entries before {opens:%H:%M} (config).")
    else:
        found["say"] = (f"WAITING  last close Rs {last['close']:,.2f} ({last['start']:%H:%M} bar), "
                        f"Rs {abs(gap):,.2f} {'under' if gap >= 0 else 'over'} {what} "
                        f"Rs {beat:,.2f} ({-gap / beat * 100:+.2f}%).")
    found["reason"] = found["say"]
    if found.get("lag_note"):
        found["say"] += f"\n{found['lag_note']}."
    else:
        live = _forming_line(found, symbol, at, traded_at)
        if live:
            found["say"] += "\n" + live
    return found


def public(found: dict) -> dict:
    """The result without the internal bar list, for JSON and for nse/entry.json."""
    return {k: v for k, v in found.items() if not k.startswith("_")}


def range_line(found: dict) -> str | None:
    """'Opening range 09:15-09:30: high .., low .. (3 of 3 bars).', or None when there is none."""
    window = found.get("opening_range") or found.get("opening_range_so_far")
    if not window:
        return None
    return (f"Opening range {window['from']}-{window['to']}: high Rs {window['high']:,.2f}, "
            f"low Rs {window['low']:,.2f} ({window['bars']} of {window['of']} bars"
            f"{'' if window.get('complete') else ', still forming'}).")


def bar_line(bar: dict, found: dict) -> str:
    """One watch line for a bar that just closed."""
    window = found.get("opening_range") or {}
    beat = found.get("level")
    rows = found.get("_rows") or []
    range_end = _hm(clock.OPEN.hour * 60 + clock.OPEN.minute + int(found.get("or_minutes") or 15))
    in_range = bar["end"].strftime("%H:%M") <= range_end
    if found.get("setup") == "orb" and in_range:
        seen = [b for b in rows if b["start"] <= bar["start"]] or [bar]
        note = (f"range so far Rs {min(b['low'] for b in seen):,.2f} to "
                f"Rs {max(b['high'] for b in seen):,.2f}")
        if window and bar["end"].strftime("%H:%M") == window.get("to"):
            note = f"opening range set: high Rs {window['high']:,.2f}, low Rs {window['low']:,.2f}"
    elif found.get("status") == "triggered" and found.get("bar_start") == f"{bar['start']:%H:%M}":
        note = (f"TRIGGERED: closed above Rs {beat:,.2f} ({found['pct_over']:+.2f}%). Get in now "
                f"if you take it: {found['trigger_time']}, Rs {found['entry']:,.2f}")
    elif beat is None:
        note = ""
    elif bar["close"] > beat:
        note = f"above Rs {beat:,.2f}"
        ref = found.get("_reference_volume")
        floor = found.get("min_volume_x")
        if not bars.traded(bar):
            note += ", but nobody traded it: one price on no volume"
        elif bar["end"].strftime("%H:%M") < found.get("no_entries_before", "00:00"):
            note += f", but no entries before {found['no_entries_before']}"
        elif floor and (not ref or bar["volume"] is None):
            note += f", but its volume is unknown, so it cannot clear your {floor:g}x floor"
        elif floor and bar["volume"] / ref < floor:
            note += f", but volume {bar['volume'] / ref:.2f}x is under {floor:g}x"
    else:
        note = f"Rs {beat - bar['close']:,.2f} under Rs {beat:,.2f}"
    return f"{bar['start']:%H:%M} bar  closed Rs {bar['close']:,.2f}  {note}"


def offline_time(symbol: str, interval: str, offline: str):
    """An offline file with no --now is read as of its own time: the feed's last trade in it.

    (time, None), or (None, why) when the file cannot be read: that is said, as the US pack says
    it, rather than replaced by today's clock, which on a weekend answered "NSE CLOSED" for a
    file that was simply broken.
    """
    try:
        bars.candles(symbol, interval, offline=offline)
    except bars.NoBars as err:
        return None, str(err)
    except READ_ERRORS as err:
        return None, _unreadable(err, offline)
    return bars.last_trade(symbol), None


def check_args(args) -> None:
    if args.side in ("sell", "short"):
        raise Refused("this times long entries only, a close ABOVE a level. A short would need "
                      "the mirror rule, which this pack does not offer")
    if args.setup == "orb" and args.level is not None:
        raise Refused("--level is for --setup level; an orb's level is its range high")
    if args.setup == "level" and args.level is None and args.mode in ("check", "watch"):
        raise Refused("--setup level needs --level PRICE, for example a 20-day high from "
                      "/nse-scan")
    if args.level is not None and args.level <= 0:
        raise Refused("--level must be a price above 0")
    if args.min_volume_x is not None and args.min_volume_x <= 0:
        raise Refused("--min-volume-x must be above 0")
    step = bars.MINUTES[args.interval]
    if args.or_minutes <= 0 or args.or_minutes % step:
        raise Refused(f"--or-minutes must be a whole number of {args.interval} bars")
    if args.every <= 0:
        raise Refused("--every must be more than zero seconds")
    if args.until:
        strict_time(args.until, "--until")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("check", "watch", "watchlist", "clock"))
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--setup", choices=("orb", "level"), default="orb")
    ap.add_argument("--level", type=show.number)
    ap.add_argument("--interval", choices=tuple(bars.MINUTES), default="5m")
    ap.add_argument("--or-minutes", type=int, default=None,
                    help="the opening range in minutes (15; watchlist: the scan's own)")
    ap.add_argument("--min-volume-x", type=show.number)
    ap.add_argument("--side", default="buy", choices=("buy", "long", "sell", "short"),
                    help="long only")
    ap.add_argument("--until", help="watch: stop at this IST time, like 15:00")
    ap.add_argument("--every", type=show.number, default=30.0,
                    help="watch: seconds between polls while the feed is behind the clock")
    ap.add_argument("--now", help="pin the clock, like '2026-10-23 10:15' or '10:15' (IST)")
    ap.add_argument("--offline", help="a saved chart JSON, or a folder of them")
    ap.add_argument("--watchlist", help="watchlist: the file to read (default nse/watchlist.json)")
    ap.add_argument("--config", help="a config file instead of the usual places")
    ap.add_argument("--write", nargs="?", const="", metavar="PATH",
                    help="check: save the result, to nse/entry.json or PATH")
    ap.add_argument("--out", help="where a trigger is written (watch writes one on a trigger)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    args.or_given = args.or_minutes is not None
    if not args.or_given:
        args.or_minutes = 15

    global CONFIG_PATH
    CONFIG_PATH = args.config
    try:
        settings = config()
    except FileNotFoundError as err:
        print(f"Refused: {err}. A config named with --config has to exist.")
        return 2
    try:
        check_config(settings)
        check_args(args)
        args.now = pinned_now(args.now)
        at = clock.now(args.now)
    except Refused as err:
        print(f"Refused: {err}.")
        return 2
    except clock.BadTime as err:
        print(f"Refused: {err}.")
        return 2

    try:
        return _run(ap, args, at)
    except KeyboardInterrupt:
        print("entry.py: stopped. Nothing was placed.", file=sys.stderr)
        return 1


def _run(ap, args, at) -> int:
    if args.mode == "clock":
        try:
            print(clock.status(at, offline=bool(args.offline)))
            if not clock.is_open(at, offline=bool(args.offline)):
                print(f"Next open: {clock.next_open(at, offline=bool(args.offline)):%a %Y-%m-%d %H:%M}"
                      f" IST")
        except clock.Unknown as err:
            print(f"{err}.")
            return 1
        return 0

    if args.mode == "watchlist":
        return watchlist(args, at)

    if not args.symbol:
        ap.error("a symbol is needed for check and watch")
    # "SBIN.NS" is the feed's name for SBIN; the exchange's symbol is what is asked for.
    symbol = args.symbol.upper().removesuffix(".NS")
    if args.offline and not args.now:
        own, why = offline_time(symbol, args.interval, args.offline)
        if why:
            print(f"{symbol}: {why}" if not why.upper().startswith(symbol) else why)
            return 1
        at = own or at
    try:
        feed_span(at.date(), args.interval, args.offline)
    except Refused as err:
        print(f"Refused: {err}.")
        return 2

    if args.mode == "check":
        found = look(symbol, args.setup, args.level, args.interval, args.or_minutes,
                     args.min_volume_x, at, args.offline)
        _report(found, args.json)
        if args.write is not None or args.out:
            where = show.hand_off("entry.json", public(found), args.out or args.write or None)
            if not args.json:
                print(f"\nwrote {where} (status {found['status']})")
        return 0 if found["status"] == "triggered" else 1

    return watch(args, symbol, at)


def _head(found: dict, at) -> str:
    """The US pack's head line: the stock, the moment, what is watched, on what bars."""
    head = (f"{found['symbol']}  {at:%a %Y-%m-%d %H:%M} IST  {setup_name(found)} on "
            f"{found['interval']} bars (free, unofficial, can lag)")
    if found.get("data_time") and found["data_time"][:10] == found["date"] \
            and found["data_time"] <= found["as_of"]:
        head += f", data to {found['data_time'][11:19]}"
    return head


def watch(args, symbol: str, at) -> int:
    """One line per closed bar until a trigger (exit 0, written), expiry, the close or --until.

    With --now or --offline it replays the day on a simulated clock and never sleeps: the clock
    steps to each bar's close, as the live loop wakes on it, and never past --until, the cutoff
    or the close, which it wakes on exactly as the US pack does.
    """
    simulated = bool(args.now or args.offline)
    # No --until means run to expiry or the close, as the US pack does. Defaulting it to the cutoff
    # stopped the watch at 15:00 while the 14:55 bar, closing AT the cutoff, was still settling.
    until = strict_time(args.until, "--until") if args.until else None
    step = bars.MINUTES[args.interval]
    printed, errors, first, cursor, said_wait = None, 0, True, at, False
    print(f"{symbol}  {cursor:%a %Y-%m-%d %H:%M} IST  watching "
          f"{'the opening-range breakout' if args.setup == 'orb' else 'a level break'} on "
          f"{args.interval} bars (free, unofficial, can lag)")
    while True:
        found = look(symbol, args.setup, args.level, args.interval, args.or_minutes,
                     args.min_volume_x, cursor, args.offline)
        rows = found.get("_rows") or []
        if found.get("_failed"):
            # A read that failed, not a session with nothing in it yet: the US pack counts only
            # these. "No closed bars yet" with the feed answering is waited out, not a failure.
            errors += 1
            print(f"{cursor:%H:%M:%S}  could not read bars: {found['say'].splitlines()[0]}")
            if errors >= MAX_ERRORS:
                print(f"Stopping after {errors} failed reads in a row.")
                return 1
            # Asked again in --every seconds, as the US pack does, not a whole bar later.
            cursor = _later(cursor, args.every, simulated)
            continue
        errors = 0
        if not rows and found["status"] in ("not_open", "closed", "forming") and not said_wait:
            line = found["say"].splitlines()[0]
            first_close = next_close(cursor.replace(hour=0, minute=0), step)
            if found["status"] != "closed" and cursor < first_close:
                line += f" Watching from the first bar's close at {first_close:%H:%M}."
            print(f"{cursor:%H:%M}  {line}")
            said_wait = True                                     # said once, as the US pack does
        fresh = False
        if first and rows:
            # Joining mid-session, as the US pack does: the head, the range, the latest bar, not
            # every bar since 09:15.
            print(_head(found, cursor))
            if range_line(found):
                print(range_line(found))
            if found["status"] != "triggered":
                print(bar_line(rows[-1], found))
            printed = rows[-1]["start"]
            first, fresh = False, True
        for bar in rows:
            if printed is None or bar["start"] > printed:
                print(bar_line(bar, found))
                printed = bar["start"]
        if found["status"] == "triggered":
            # The TRIGGERED sentence is the trigger bar's own line already, unless this is the
            # first look, where no bar line was printed for it.
            for line in found["say"].splitlines()[0 if fresh else 1:]:
                print(line)
            print("Next: set the stop and target BEFORE you buy: /nse-exit")
            where = show.hand_off("entry.json", public(found), args.out)
            print(f"wrote {where}.")
            return 0
        if found["status"] == "expired":
            print(found["say"])
            return 1
        if found["status"] == "closed" or cursor.time() >= clock.CLOSE:
            print(f"Stopped at {cursor:%H:%M}: the market is closed. Nothing was placed.")
            return 1
        if until is not None and cursor.time() >= until:
            print(f"Stopped at --until {until:%H:%M} without a trigger. "
                  f"{found.get('reason') or found['say']}")
            return 1
        if found.get("lag_note"):
            # The feed is behind a bar that has closed: ask again every --every seconds, as the US
            # pack does, rather than sleep to the next bar and report the trigger a bar late.
            cursor = _later(cursor, args.every, simulated)
            continue
        due = next_close(cursor, step)
        shuts = _as_time(found.get("no_entries_after"), NO_ENTRIES_AFTER)
        for stop_at in (until, shuts, clock.CLOSE):
            if stop_at is not None:
                moment = cursor.replace(hour=stop_at.hour, minute=stop_at.minute, second=0,
                                        microsecond=0)
                if cursor < moment < due:
                    due = moment                                 # wake on it, never past it
        if simulated:
            cursor = due
        else:
            cursor = _wait_for(symbol, args.interval, step, due, args.every)


def _later(cursor, seconds: float, simulated: bool):
    """The clock `seconds` from now: stepped when replaying, slept when live."""
    if simulated:
        return cursor + timedelta(seconds=seconds)
    _time.sleep(seconds)
    return clock.now()


def _bad_level(level) -> str | None:
    """Why a watchlist level is not a price, or None when it is one."""
    if isinstance(level, bool):
        return f"the watchlist's level {level!r} is not a number"
    try:
        value = float(level)
    except (TypeError, ValueError):
        return f"the watchlist's level {level!r} is not a number"
    if not math.isfinite(value) or value <= 0:
        return f"the watchlist's level {level!r} is not a price above 0"
    return None


def watchlist(args, at) -> int:
    """Every hit the scanner wrote, checked now. A level setup takes each hit's own level."""
    path = Path(args.watchlist) if args.watchlist else show.shared("watchlist.json")
    if not path.exists():
        print(f"No {path}. Run /nse-scan first, or name a symbol.")
        return 2
    if args.offline and not Path(args.offline).is_dir():
        print("Refused: watchlist --offline wants a folder of saved charts, one per symbol; a "
              "single file holds one stock.")
        return 2
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as err:
        print(f"Refused: {path} is not valid JSON: {err}.")
        return 2
    listed = saved.get("hits") if isinstance(saved, dict) else saved
    hits = [h for h in (listed if isinstance(listed, list) else [])
            if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        print(f"No hits in {path}: the scanner found nothing to watch.")
        return 2
    # The scan's own range, unless --or-minutes says otherwise: a 30-minute scan's hits re-checked
    # on a 15-minute range are different breakouts.
    minutes = args.or_minutes
    if not args.or_given and isinstance(saved, dict) and saved.get("or_minutes") is not None:
        minutes = saved["or_minutes"]
        step = bars.MINUTES[args.interval]
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0 \
                or minutes % step:
            print(f"Refused: {path} gives or_minutes {minutes!r}, not a whole number of "
                  f"{args.interval} bars. Pass --or-minutes.")
            return 2
    if not args.offline:
        try:
            feed_span(at.date(), args.interval, None)
        except Refused as err:
            print(f"Refused: {err}.")
            return 2
    rows = []
    for number, hit in enumerate(hits):
        symbol = str(hit["symbol"]).upper().removesuffix(".NS")
        level = None
        if args.setup == "level":
            level = args.level if args.level is not None else hit.get("level")
            if level is None:
                rows.append((symbol, None, "needs a level: --level PRICE, or a \"level\" in the "
                                           "watchlist"))
                continue
            wrong = _bad_level(level)
            if wrong:
                rows.append((symbol, None, wrong))
                continue
            level = float(level)
        when = at
        if args.offline and not args.now:
            when, why = offline_time(symbol, args.interval, args.offline)
            if why:
                rows.append((symbol, None, why))
                continue
        if number and not args.offline:
            _time.sleep(PAUSE)
        found = look(symbol, args.setup, level, args.interval, minutes,
                     args.min_volume_x, when or at, args.offline)
        rows.append((symbol, found, None))
    if args.json:
        print(json.dumps([{"symbol": s, "result": public(f) if f else None, "error": e}
                          for s, f, e in rows], indent=2, default=str))
    else:
        print(f"{'symbol':<12} {'status':<10} {'time':<6} {'price':>10} {'level':>10} "
              f"{'last':>10} {'vs level':>9}")
        for symbol, found, error in rows:
            if not error and found["status"] == "no_data":
                error = found["say"].splitlines()[0] if found.get("say") else "no data"
            if error:
                print(f"{symbol:<12} error: {error}")
                continue
            last = found.get("last") or {}
            entry = format(found["entry"], ",.2f") if found.get("entry") else "-"
            level = format(found["level"], ",.2f") if found.get("level") else "-"
            close = format(last["close"], ",.2f") if last else "-"
            versus = format(last["vs_level_pct"], "+.2f") + "%" if last else "-"
            print(f"{symbol:<12} {found['status']:<10} {found.get('trigger_time') or '-':<6} "
                  f"{entry:>10} {level:>10} {close:>10} {versus:>9}")
        done = sum(1 for _, f, _ in rows if f and f["status"] == "triggered")
        print(f"{done} of {len(rows)} triggered. Pick one, then: entry.py check SYMBOL --write, "
              f"and /nse-exit.")
        print("Free bars, unofficial, and they can lag. Nothing was placed.")
    return 0 if any(f and f["status"] == "triggered" for _, f, _ in rows) else 1


def next_close(now, step_minutes: int):
    """When the bar forming at `now` closes. NSE's bars are aligned to 09:15, not to the hour."""
    opened = now.replace(hour=clock.OPEN.hour, minute=clock.OPEN.minute, second=0, microsecond=0)
    if now < opened:
        return opened + timedelta(minutes=step_minutes)
    done = int((now - opened).total_seconds() // (step_minutes * 60)) + 1
    return opened + timedelta(minutes=done * step_minutes)


def _wait_for(symbol: str, interval: str, step: int, due, every: float):
    """Sleep until just after `due`, then, if it is a bar's close, poll until the feed has it.

    The feed is free and lags. Waking exactly on the close and reading straight away often finds
    the bar not there yet, which would read as "nothing happened". So it waits a few seconds past
    the close, then asks again every `every` seconds for up to a minute. After that it carries on
    with what it has rather than stall the session. A bar that is there but whose close the feed's
    last trade has not reached yet is handled by watch(), which asks again every `every` seconds.
    A wake that is --until, the cutoff or the close and not a bar's close is not polled for.
    """
    now = clock.now()
    on_grid = next_close(due - timedelta(seconds=1), step) == due
    _time.sleep(max(0.0, (due - now).total_seconds()) + (GRACE if on_grid else 0))
    if not on_grid:
        return clock.now()
    deadline = clock.now() + timedelta(seconds=60)
    while clock.now() < deadline:
        try:
            rows = bars.candles(symbol, interval, at=clock.now())
            if rows and rows[-1]["end"] >= due:
                break
        except (bars.NoBars,) + READ_ERRORS:
            pass                                 # said by the look that follows, not here
        _time.sleep(every)
    return clock.now()


def _report(found: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(public(found), indent=2, default=str))
        return
    print(_head(found, datetime.fromisoformat(found["as_of"])))
    if range_line(found):
        print(range_line(found))
    if found.get("range_note"):
        print(found["range_note"])
    print(found["say"])
    if found["status"] == "triggered":
        print("Next: set the stop and target BEFORE you buy: /nse-exit")


if __name__ == "__main__":
    show.run(main)
