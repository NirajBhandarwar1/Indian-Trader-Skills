"""Intraday bars for NSE stocks, with no key and no account.

Measured: the same free keyless chart endpoint that carries US stocks carries Indian ones with a
.NS suffix. RELIANCE.NS and SUZLON.NS both answer 200 with 75 five-minute bars a day and 375
one-minute bars, timezone Asia/Kolkata, currency INR -- which is exactly NSE's 09:15 to 15:30
session, so nothing is missing from the middle or the ends.

ONLY CLOSED BARS COUNT. The endpoint's last bar is the one forming right now, and its high is
whatever the price has touched so far. Treating that as a bar turns "the price poked above the
level" into "the level broke", which is the single easiest way to call an entry that never
happened. candles() drops it unless the clock says it has finished.

Standard library only.
"""
from __future__ import annotations

import gzip
import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .clock import CLOSE, IST, OPEN

CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
FALLBACK_HOST = "https://query2.finance.yahoo.com/v8/finance/chart"
USER_AGENT = "nse-trader-skills/1.0 (+https://github.com/)"
BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/122.0.0.0 Safari/537.36")

MINUTES = {"1m": 1, "5m": 5, "15m": 15}


class NoBars(Exception):
    """No intraday bars for that symbol. For a fresh listing or a suspended stock that is right."""


class NoClosedBars(NoBars):
    """The chart was read, and no bar of the session has closed yet by the clock."""


# ---- what a bar's volume actually tells you -----------------------------------------------------
#
# THE FEED REPORTS ZERO VOLUME FOR TWO DIFFERENT THINGS, and treating them alike is false data.
#
# Measured on five liquid stocks over five sessions: the 09:15 bar shows zero volume EVERY day, for
# every one of them, while its price moves -- SBIN's 09:15 bar on 2026-10-22 ran from 950.05 to
# 959.30, and nothing moves a price except trades. The exchange's own daily file agrees: the bars
# add up to 2.7% to 8.3% LESS than the day's real volume (SBIN 8,976,957 against 9,734,968). So
# that zero is volume the feed did not report. It is unknown, not nothing.
#
# A bar whose open, high, low and close are all the same price with zero volume is the other case:
# nobody traded, and the feed carried the last price forward. That zero is real.
#
# So a zero with a price range becomes None here, and every caller has to treat it as unknown --
# never averaged in as a zero, never shown as a ratio of 0.0.

def volume_or_unknown(bar: dict) -> int | None:
    """The bar's volume, or None where the feed reported zero for a bar that clearly traded."""
    volume = bar.get("volume")
    if volume:
        return int(volume)
    if bar.get("high") is not None and bar.get("low") is not None and bar["high"] != bar["low"]:
        return None
    return 0


def traded(bar: dict) -> bool:
    """Whether anything traded in the bar. A volume, or a price range, which only trades make."""
    return bool(bar.get("volume")) or (bar.get("high") != bar.get("low"))


def known_volume(rows: list[dict]) -> list[int]:
    """The volumes that are actually known, leaving out the ones the feed did not report."""
    return [b["volume"] for b in rows if b.get("volume") is not None]


BACKOFF = 5.0      # seconds before the one retry after an HTTP 429, as the US pack waits


def _get(url: str, timeout: float) -> bytes:
    """One chart request, the US pack's way: the first host, then the second on a 429 or a 5xx.

    A 429 (too many requests) gets one retry on the same host after a pause. A 404 is the feed
    saying it has no such symbol, which is reported as that rather than as a network fault. A
    timeout or a dropped connection is a refusal to answer, never a crash.
    """
    import time as _time
    last = "no answer"
    for host in (CHART, FALLBACK_HOST):
        for attempt in (0, 1):
            req = urllib.request.Request(url.replace(CHART, host), headers={
                "User-Agent": BROWSER, "Accept": "*/*", "Accept-Encoding": "gzip, deflate",
                "Referer": "https://finance.yahoo.com/", "X-Client": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return body
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    raise NoBars("the feed has no such symbol (HTTP 404): check the NSE symbol, "
                                 "without the .NS") from err
                last = f"HTTP {err.code}"
                if err.code == 429 and attempt == 0:
                    _time.sleep(BACKOFF)
                    continue
                if err.code == 429 or err.code >= 500:
                    break                                        # try the other host
                raise NoBars(f"the feed answered HTTP {err.code}") from err
            except urllib.error.URLError as err:
                last = f"could not connect: {err.reason}"
                break
            except OSError as err:                               # a timeout mid-read
                last = f"network error: {err}"
                break
    raise NoBars(f"the bars could not be fetched ({last} on both hosts): wait a minute and try "
                 f"again")


# The feed's own time of its last trade, per symbol, from the most recent candles() call. A bar
# whose close time has passed on the clock can still change until the feed has caught up to it,
# and this is how a caller tells: see nse-entry. None when the feed did not say.
LAST_TRADE: dict[str, datetime | None] = {}


def last_trade(symbol: str) -> datetime | None:
    return LAST_TRADE.get(symbol.upper())


# The bar still being built at `at`, per symbol, from the most recent candles() call that was
# given a clock: what the feed says of it so far. Kept so a caller can name it without asking the
# feed a second time. It is never a closed bar and never a trigger. None when there is none.
FORMING: dict[str, dict | None] = {}


def forming_bar(symbol: str) -> dict | None:
    return FORMING.get(symbol.upper())


# The start of the newest bar the feed holds for the session asked for, closed or still forming,
# per symbol, from the most recent candles() call. None when it held none.
NEWEST: dict[str, datetime | None] = {}


def newest_start(symbol: str) -> datetime | None:
    return NEWEST.get(symbol.upper())


# What the most recent candles() call threw away, per symbol, by why: see candles(). None when
# that call failed before reading any bars.
DROPPED: dict[str, dict | None] = {}


def dropped_counts(symbol: str) -> dict | None:
    return DROPPED.get(symbol.upper())


def _countable(volume) -> bool:
    """A volume that can be read as a count: missing (the feed's null, read as zero as before),
    or a finite number of at least zero. A nan, an infinity or a negative is not a count, and the
    bar's volume is then unknown -- never a crash that loses the whole session, never a zero."""
    if volume is None:
        return True
    if isinstance(volume, bool) or not isinstance(volume, (int, float)):
        return False
    return math.isfinite(volume) and volume >= 0


def candles(symbol: str, interval: str = "5m", day: str | None = None,
            at: datetime | None = None, offline: str | None = None,
            timeout: float = 30, span: str = "5d", dropped: dict | None = None) -> list[dict]:
    """Closed bars for one NSE symbol on one session, oldest first.

    What was thrown away is counted, never silently lost: pass `dropped={}` and it is filled with
    {"missing_price", "off_grid", "outside_hours", "duplicate", "no_time"}, or read
    dropped_counts(symbol) after the call. Bars of other sessions than the one asked for are not
    counted, and neither is a bar still forming (that is FORMING, not a loss).

    Each bar is {"start", "end", "open", "high", "low", "close", "volume"} with IST times. `at`
    is the clock: a bar is kept only once `at` has passed its end, so a run at 10:12 sees the
    10:05 bar and not the 10:10 one that is still forming.

    `span` is how far back to ask. Five days is enough for today's session; the time machine asks
    for 60, which is what the free chart endpoint keeps of five-minute bars. Measured on
    2026-10-24: 60d returned 60 SBIN sessions of 75 bars each, 2026-07-29 to 2026-10-23.
    """
    if interval not in MINUTES:
        raise NoBars(f"{interval!r} is not one of {', '.join(MINUTES)}")
    # Said afresh by every call: a read that fails must not leave the last one's answer behind.
    LAST_TRADE[symbol.upper()] = None
    FORMING[symbol.upper()] = None
    NEWEST[symbol.upper()] = None
    DROPPED[symbol.upper()] = None
    if dropped is not None:
        dropped.clear()

    if offline:
        # A file, or a FOLDER of saved responses. The folder form matters for a watchlist: one
        # --offline pointing at a directory replays every symbol in it, where a file can only
        # ever replay one.
        where = Path(offline)
        if where.is_dir():
            found = ([where / f"{symbol.upper()}-{span}{interval}.json",
                      where / f"{symbol.upper()}-{span}{interval}.json.gz"]
                     if span != "5d" else [])
            found += [where / f"{symbol.upper()}-{interval}.json",
                     where / f"{symbol.upper()}-{interval}.json.gz",
                     where / f"{symbol.upper()}.json",
                     where / f"{symbol.upper()}.json.gz"]
            where = next((p for p in found if p.exists()), None)
            if where is None:
                raise NoBars(f"{symbol}: nothing saved for it in {offline}")
        # A damaged file raises what reading it raised (OSError, ValueError, AttributeError...):
        # each skill words that refusal itself, so it is not turned into "no bars" here.
        opener = gzip.open if str(where).endswith(".gz") else open
        with opener(where, "rt", encoding="utf-8") as handle:
            parsed = json.load(handle)
    else:
        ticker = f"{symbol.upper()}.NS"
        parsed = json.loads(_get(f"{CHART}/{ticker}?range={span}&interval={interval}", timeout))

    result = (((parsed or {}).get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise NoBars(f"{symbol}: no bars reported")
    # The file must be the stock and the bar size asked for, as the US pack checks. Measured
    # before this check: "check ITC" on SBIN's saved file reported SBIN's breakout as ITC's, and a
    # one-minute file read as five-minute bars gave a wrong range and a wrong trigger.
    meta = result.get("meta") or {}
    held = str(meta.get("symbol") or "").upper()
    if held and held.removesuffix(".NS").removesuffix(".BO") != symbol.upper():
        raise NoBars(f"the chart holds {held}, not {symbol.upper()}")
    size = meta.get("dataGranularity")
    if size and size != interval:
        raise NoBars(f"{symbol.upper()}: the chart has {size} bars, not {interval}: use "
                     f"--interval {size}")
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    traded_at = (result.get("meta") or {}).get("regularMarketTime")
    LAST_TRADE[symbol.upper()] = (datetime.fromtimestamp(traded_at, IST)
                                  if isinstance(traded_at, (int, float)) else None)

    step = timedelta(minutes=MINUTES[interval])
    want = day or (at.date().isoformat() if at else None)
    out = []
    thrown = {"missing_price": 0, "off_grid": 0, "outside_hours": 0, "duplicate": 0,
              "no_time": 0}
    DROPPED[symbol.upper()] = thrown
    for i, stamp in enumerate(stamps):
        if stamp is None:
            thrown["no_time"] += 1
            continue
        start = datetime.fromtimestamp(stamp, IST)
        if want and start.date().isoformat() != want:
            continue                                             # another session: not asked for
        if int(stamp) % (MINUTES[interval] * 60):
            thrown["off_grid"] += 1
            continue                                             # the live tick, off the bar grid
        if not (OPEN <= start.time() < CLOSE):
            thrown["outside_hours"] += 1
            continue
        values = {k: (quote.get(k) or [None] * len(stamps))[i]
                  for k in ("open", "high", "low", "close", "volume")}
        if any(values[k] is None or (isinstance(values[k], float) and not math.isfinite(values[k]))
               for k in ("open", "high", "low", "close")):
            # A gap in the feed, not a bar. A price that is missing, or nan, is never filled in
            # from the others: that would be a bar the feed did not send. (A missing open or low
            # used to come through as None and crash the range's min() further on.)
            thrown["missing_price"] += 1
            continue
        end = start + step
        raw = values["volume"]
        bar = {"start": start, "end": end,
               "open": _round(values["open"]), "high": _round(values["high"]),
               "low": _round(values["low"]), "close": _round(values["close"]),
               "volume": int(raw or 0) if _countable(raw) else None}
        if bar["volume"] is not None:
            bar["volume"] = volume_or_unknown(bar)
        # The newest bar the feed holds for the session, closed or not. A bar whose close the
        # feed's last trade has not reached counts anyway once a later bar exists, as the US pack
        # counts it: a later bar is the feed saying this one is finished.
        if NEWEST.get(symbol.upper()) is None or start > NEWEST[symbol.upper()]:
            NEWEST[symbol.upper()] = start
        if at is not None and end > at:
            if start <= at:
                FORMING[symbol.upper()] = bar                    # named, never counted
            continue                                             # still forming: never a bar
        out.append(bar)
    # One bar a slot, oldest first: a bar the feed sent twice keeps its later copy, as the US pack
    # keeps it. Two copies had counted one bar twice in the range and in every bar number after.
    kept = {b["start"]: b for b in out}
    thrown["duplicate"] = len(out) - len(kept)
    out = sorted(kept.values(), key=lambda b: b["start"])
    if dropped is not None:
        dropped.clear()
        dropped.update(thrown)
    if not out:
        raise NoClosedBars(f"{symbol}: no closed bars for {want or 'that session'}")
    return out


SPARK ="https://query1.finance.yahoo.com/v7/finance/spark"

# Measured, not read off documentation: 20 symbols are accepted and 21 are answered 400.
SPARK_MAX = 20


def _saved_answers(offline: str) -> list:
    """Every saved batch answer in a file or a folder, or NoBars saying plainly why not.

    .json and .json.gz only: a folder of saved responses may also hold notes about where each
    came from, and those are text, not responses. A missing folder, a folder with no answers in
    it, or a damaged file is said, the way the US pack says it, rather than read as a market in
    which nothing came back -- or a traceback.
    """
    where = Path(offline)
    if not where.exists():
        raise NoBars(f"no saved batch answers at {where}")
    files = (sorted(p for p in where.iterdir() if p.name.endswith((".json", ".json.gz")))
             if where.is_dir() else [where])
    if not files:
        raise NoBars(f"no saved batch answers (.json) in {where}")
    out = []
    for path in files:
        opener = gzip.open if str(path).endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                body = json.load(handle)
        except (OSError, ValueError, EOFError) as err:
            raise NoBars(f"{path.name} is not a readable saved answer: {err}") from None
        # Valid JSON that is not a batch answer (a list, a full chart, a note) is said, the way
        # the US pack refuses an offline file of the wrong shape, never read as "nothing came
        # back" and never a traceback.
        inner = body.get("data", body) if isinstance(body, dict) and "_source" in body else body
        if not (isinstance(inner, dict) and isinstance(inner.get("spark"), dict)):
            raise NoBars(f"{path.name} is not a saved batch answer (no spark result in it)")
        out.append(body)
    return out


def _results(body) -> list[dict]:
    """The per-symbol results of one batch answer, saved or live, whatever shape came back."""
    body = body.get("data", body) if isinstance(body, dict) and "_source" in body else body
    spark_ = body.get("spark") if isinstance(body, dict) else None
    found = spark_.get("result") if isinstance(spark_, dict) else None
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


def _series(result: dict) -> dict:
    """A result's one series, or {} when the feed sent the symbol with no data."""
    response = result.get("response")
    first = response[0] if isinstance(response, list) and response else None
    return first if isinstance(first, dict) else {}


def _price(value) -> bool:
    """A close that is a real number. null is a gap; NaN would make every comparison False."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _stamps_closes(series: dict) -> tuple[list, list]:
    """A series' timestamps and closes, as lists, whatever shape the feed sent them in."""
    stamps, found = series.get("timestamp"), series.get("indicators")
    quote = found.get("quote") if isinstance(found, dict) else None
    first =quote[0] if isinstance(quote, list) and quote and isinstance(quote[0], dict) else {}
    closes = first.get("close")
    return (stamps if isinstance(stamps, list) else [],
            closes if isinstance(closes, list) else [])


def _fetch_batches(names: list[str], query: str, pause: float, timeout: float,
                   failed: dict | None, progress=None) -> list:
    """Live batch requests, twenty symbols each, a polite pause between them.

    One bad batch is not a stop, but it is counted: never a quiet "nothing broke".
    """
    import time as _time
    responses = []
    total = -(-len(names) // SPARK_MAX)
    for number, start in enumerate(range(0, len(names), SPARK_MAX)):
        chunk = names[start:start + SPARK_MAX]
        batch = ",".join(f"{s}.NS" for s in chunk)
        try:
            responses.append(json.loads(_get(f"{SPARK}?symbols={batch}&{query}", timeout)))
        except (NoBars, json.JSONDecodeError) as err:
            if failed is not None:
                failed.update({s: f"the batch request failed: {err}" for s in chunk})
        if progress is not None:
            progress(number + 1, total)
        if start + SPARK_MAX < len(names):
            _time.sleep(pause)
    return responses


def spark(symbols: list[str], interval: str = "5m", day: str | None = None,
          at: datetime | None = None, offline: str | None = None, pause: float = 0.5,
          timeout: float = 30, failed: dict | None = None,
          progress=None) -> dict[str, list[dict]]:
    """Closed bars for many symbols at once, CLOSES ONLY, twenty symbols a request.

    This is what lets a scan cover the market: 2,359 companies in about 118 requests instead of
    2,359. The price of that is the shape. Measured, the batch endpoint returns a close and nothing
    else -- no high, no low, no volume -- so it cannot apply a rule that needs a bar's high. It can
    only SHORTLIST, and candles() confirms. See nse-scan for why that loses nothing.

    `offline` is a saved batch response, or a folder of them; every symbol in every file is read,
    so how the saved batches were split does not matter.

    `failed`, when given, collects {symbol: why} for every symbol that came back with nothing: a
    batch the feed refused, or a symbol it left out of its answer. A failed request must never
    read as "nothing broke out", which is what it did when the failures were only skipped.

    `progress`, when given, is called as progress(done, total) after each live request, so a
    whole-market fetch can say how far it has got, as the US pack does.
    """
    if interval not in MINUTES:
        raise NoBars(f"{interval!r} is not one of {', '.join(MINUTES)}")
    step = timedelta(minutes=MINUTES[interval])
    want = day or (at.date().isoformat() if at else None)
    wanted = {s.upper() for s in symbols}

    responses = []
    if offline:
        responses = _saved_answers(offline)
    else:
        responses = _fetch_batches(sorted(wanted), f"range=5d&interval={interval}", pause,
                                   timeout, failed, progress)

    out: dict[str, list[dict]] = {}
    for body in responses:
        for result in _results(body):
            symbol = str(result.get("symbol") or "").upper().removesuffix(".NS")
            if symbol not in wanted:
                continue
            series = _series(result)
            stamps, closes = _stamps_closes(series)
            rows = {}
            for stamp, close in zip(stamps, closes):
                if not _price(close) or not _price(stamp):
                    continue
                if int(stamp) % int(step.total_seconds()):
                    continue                                     # the live tick, off the bar grid
                begin = datetime.fromtimestamp(stamp, IST)
                if want and begin.date().isoformat() != want:
                    continue
                if not (OPEN <= begin.time() < CLOSE):
                    continue
                if at is not None and begin + step > at:
                    continue                                     # still forming: never a bar
                # One bar a slot, the later copy winning, as the US pack keeps them.
                rows[begin] = {"start": begin, "end": begin + step, "close": _round(close)}
            if rows:
                out[symbol] = [rows[k] for k in sorted(rows)]
    if failed is not None:
        for symbol in sorted(wanted - set(out)):
            failed.setdefault(symbol, f"no closed bars for {want or 'that session'} in the batch "
                                      f"answer")
    return out


def spark_daily(symbols: list[str], at: datetime | None = None, offline: str | None = None,
                pause: float = 0.5, timeout: float = 30,
                failed: dict | None = None, progress=None) -> dict[str, dict]:
    """About three months of daily CLOSES for many symbols, twenty a request, with the day's volume.

    What the US pack's pre-market scan runs on, and it needs no cache at all. Measured for NSE:
    63 to 64 trading days a symbol, and the day's volume beside it -- SBIN's 9,733,931 against the
    exchange's own 9,734,968 for the same day.

    TODAY'S BAR IS DROPPED UNTIL THE SESSION CLOSES. During the session the last "daily close" is
    only the latest trade, and a rule that fires on it has fired on a price that may not last.
    Anything dated after the clock is dropped too.
    """
    at = at or datetime.now(IST)
    wanted = {s.upper() for s in symbols}
    responses = []
    if offline:
        responses = _saved_answers(offline)
    else:
        responses = _fetch_batches(sorted(wanted), "range=3mo&interval=1d", pause, timeout,
                                   failed, progress)

    done = at.time() >= CLOSE
    out: dict[str, dict] = {}
    for body in responses:
        for result in _results(body):
            symbol = str(result.get("symbol") or "").upper().removesuffix(".NS")
            if symbol not in wanted:
                continue
            series = _series(result)
            if not series:
                # Named in the answer with no data: a failed fetch, as the US pack counts it,
                # never a chart that came back and then failed the liquidity floor.
                continue
            meta = series.get("meta") if isinstance(series.get("meta"), dict) else {}
            stamps, closes = _stamps_closes(series)
            days = {}
            for stamp, close in zip(stamps, closes):
                if not _price(close) or not _price(stamp):
                    continue
                day = datetime.fromtimestamp(stamp, IST).date()
                if day > at.date() or (day == at.date() and not done):
                    continue
                # One close a day, the later copy winning: the feed can send the day's bar and
                # then the live tick dated the same day, and two closes for one session would
                # read as a flat day and break every rule that compares a close with the last.
                days[day.isoformat()] = _round(close)
            price, volume = meta.get("regularMarketPrice"), meta.get("regularMarketVolume")
            out[symbol] = {"closes": sorted(days.items()),
                           "price": price if _price(price) else None,
                           "volume": volume if _price(volume) else None}
    if failed is not None:
        for symbol in sorted(wanted - set(out)):
            failed.setdefault(symbol, "no data for it in the batch answer")
    return out


def forming(symbol: str, interval: str = "5m", at: datetime | None = None,
            offline: str | None = None, timeout: float = 30) -> dict | None:
    """The bar still being built, for saying how far the price is from a level. Never a trigger."""
    try:
        every = candles(symbol, interval, at=None, offline=offline, timeout=timeout)
    except NoBars:
        return None
    if at is None:
        return None
    live = [b for b in every if b["start"].date() == at.date() and b["end"] > at
            and b["start"] <= at]
    return live[-1] if live else None


def opening_range(rows: list[dict], minutes: int = 15, at: datetime | None = None) -> dict | None:
    """The high and low of the session's first `minutes`.

    NSE opens 09:15, so a 15-minute range is the 09:15, 09:20 and 09:25 five-minute bars and ends
    at 09:30. THE RANGE IS ANCHORED TO 09:15 BY THE CLOCK, never to the first bar the feed sent:
    measured on a real SBIN file with its 09:15 bar blanked, anchoring to the first bar built the
    range from 09:20 to 09:35, swallowing the 09:30 bar and moving the high from 958.15 to 959.45.

    Returns None until the range has ended: half a range is not a range. It has ended once `at`
    has passed 09:15 + `minutes`, or, with no `at`, once its last bar or a later bar has closed.
    A bar the feed never sent inside an ended range does not hold the range open forever -- with
    the real 09:25 bar blanked it had said "still forming" all day -- so the range is built from
    the bars there are, and `bars` against `of`, with `missing` naming the absent bars, says so.
    """
    if not rows:
        return None
    first = rows[0]["start"].replace(hour=OPEN.hour, minute=OPEN.minute, second=0, microsecond=0)
    ends = first + timedelta(minutes=minutes)
    step = rows[0]["end"] - rows[0]["start"]
    inside = [b for b in rows if first <= b["start"] < ends]
    if at is not None:
        ended = at >= ends
    else:
        ended = any(b["start"] >= ends for b in rows) or bool(inside and inside[-1]["end"] == ends)
    if not inside or not ended:
        return None
    slots = [first + step * k for k in range(int((ends - first) / step))]
    have = {b["start"] for b in inside}
    missing = [s.strftime("%H:%M") for s in slots if s not in have]
    # The average volume of the range, over the bars whose volume is KNOWN. The 09:15 bar's volume
    # is never reported, so counting it as zero cut the average by a third and made every volume
    # ratio built on it half as large again as it really was. Measured on 2026-10-22: SBIN's break
    # read 1.66x when it was 1.10x, ITC 0.87x when it was 0.58x, SUZLON 0.90x when it was 0.60x.
    # With no known volume at all the average is unknown, not zero.
    seen = known_volume(inside)
    return {"high": max(b["high"] for b in inside), "low": min(b["low"] for b in inside),
            "bars": len(inside), "of": len(slots), "missing": missing, "from": first, "to": ends,
            "volume": (sum(seen) / len(seen)) if seen else None,
            "volume_bars": len(seen)}


def atr(rows: list[dict], window: int = 14, places: int | None = 4) -> tuple[float, int] | None:
    """The average true range over the last `window` bars, and how many it used.

    With fewer than `window` closed bars it averages the ones there are and says so, rather than
    returning nothing: early in a session that is the only honest answer.

    `places` rounds the average for showing (4). None returns it unrounded, for a caller that
    does its own rounding: nse-exit sets a stop from it at 6 places, as the US pack does, and a
    value already cut to 4 places can land a stop one tick away from that.
    """
    if not rows:
        return None
    # The US pack's true range: the session's first bar has no bar before it, so its true range is
    # its own high less its low; every later bar also reaches back to the close before it. The
    # first version left the first bar out entirely, which early in the day is one bar in three.
    trues, before = [], None
    for bar in rows:
        tr = bar["high"] - bar["low"]
        if before is not None:
            tr = max(tr, abs(bar["high"] - before["close"]), abs(bar["low"] - before["close"]))
        trues.append(tr)
        before = bar
    used = trues[-window:]
    mean = sum(used) / len(used)
    return (mean if places is None else round(mean, places)), len(used)


def _round(value):
    return None if value is None else round(float(value), 2)
