"""Free delayed quotes for NSE stocks, with no key: the last price and the previous close.

Shared by /nse-alerts (which rings on them) and /nse-news (which says how far each stock has moved
today). DELAYED AND UNOFFICIAL: fine for an alert or a news line, never for timing an order.

Standard library only.
"""
from __future__ import annotations

import gzip
import json
import math
import time as _time
import urllib.error
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path

from . import clock

QUOTE = "https://query1.finance.yahoo.com/v8/finance/chart"
BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/122.0.0.0 Safari/537.36")
PAUSE = 0.5
BACKOFF = 2.0   # seconds before the one retry after a 429, as the US pack waits


class NoQuote(Exception):
    """No price for that symbol right now."""


def quote(symbol: str, timeout: float = 20, offline: str | None = None,
          source: str = "feed") -> dict:
    """The last price and the previous close, from a free keyless endpoint.

    DELAYED AND UNOFFICIAL. Fine for an alert and for checking a size; never for timing an order.
    The quote's own time is carried so a stale one can be spotted rather than trusted.

    Two sources, tried in turn with `source="feed"`, and the SECOND IS THE EXCHANGE'S OWN daily
    file. That ordering is deliberate: the first is quicker and intraday, but the exchange's
    bhavcopy is the record, so if the quick one is down the fallback is better data rather than
    worse -- it is just a day old, and it says so. `source="exchange"` asks the exchange only.
    """
    if offline and source == "exchange":
        # Offline never fetches, and the exchange's file is a live download.
        raise NoQuote(f"{symbol.upper()}: --source exchange is a live download; an offline "
                      f"replay reads the saved feed quotes (drop --source exchange)")
    if source == "exchange":
        return from_exchange(symbol)
    if offline:
        where = Path(offline, f"{symbol.upper()}.json")
        packed = where.with_name(where.name + ".gz")
        if where.exists():
            body = where.read_bytes()
        elif packed.exists():
            try:
                body = gzip.decompress(packed.read_bytes())
            except (OSError, EOFError, zlib.error):
                # zlib.error: a gzip header over a damaged body, which is neither of the others.
                raise NoQuote(f"the offline quote file {packed} is not a readable gzip "
                              f"file") from None
        else:
            raise NoQuote(f"no offline quote file {where}")
    else:
        ticker = f"{symbol.upper()}.NS"
        body = None
        for host in (QUOTE, QUOTE.replace("query1", "query2")):
            body = _fetch(f"{host}/{ticker}?range=1d&interval=5m", timeout)
            if body is not None:
                break
        if body is None:
            return from_exchange(symbol)

    try:
        chart = (json.loads(body) or {}).get("chart") or {}
        result = chart.get("result")
        if result is not None and not isinstance(result, list):
            raise TypeError                     # {"result": {...}} crashed on [0] as a KeyError
        first = (result or [{}])[0]
        meta = first.get("meta") or {}
        if not isinstance(meta, dict):
            raise TypeError
    except (ValueError, AttributeError, IndexError, TypeError):
        raise NoQuote(f"the feed returned something unexpected for {symbol.upper()}") from None
    if not result:
        # The feed's own answer for a ticker it does not know, said as it says it.
        said = chart.get("error") if isinstance(chart.get("error"), dict) else {}
        raise NoQuote(f"the feed has no data for {symbol.upper()}: "
                      f"{said.get('description') or 'its answer held no result'}")
    held = str(meta.get("symbol") or "").upper()
    if held and held.removesuffix(".NS").removesuffix(".BO") != symbol.upper():
        raise NoQuote(f"the quote file holds {held}, not {symbol.upper()}")
    price = _positive(meta.get("regularMarketPrice"))
    if price is None:
        # Missing, zero, negative, text or not finite: none of them is a price, and a stop at
        # Rs 960 "rang" on a price of 0.
        raise NoQuote(f"{symbol.upper()}: no usable price reported "
                      f"({meta.get('regularMarketPrice')!r})")
    previous = _positive(meta.get("previousClose"))
    if previous is None:
        # With range=1d the chart's previous close IS the previous session's close.
        previous = _positive(meta.get("chartPreviousClose"))
    # The day's open is a help for pct_move, never a reason to refuse the price: a part of the
    # answer in another shape is read as absent, where it used to crash the whole run.
    indicators = first.get("indicators")
    bars = indicators.get("quote") if isinstance(indicators, dict) else None
    bars = bars[0] if isinstance(bars, list) and bars and isinstance(bars[0], dict) else {}
    opened = bars.get("open")
    opens = [x for x in (_positive(v) for v in (opened if isinstance(opened, list) else []))
             if x is not None]
    return {"symbol": symbol.upper(), "price": round(price, 2),
            "previous_close": round(previous, 2) if previous is not None else None,
            "open": round(opens[0], 2) if opens else None,
            "high": _rounded(meta.get("regularMarketDayHigh")),
            "low": _rounded(meta.get("regularMarketDayLow")),
            "at": _stamp(meta.get("regularMarketTime")),
            "currency": meta.get("currency"), "source": "intraday feed"}


def _positive(value) -> float | None:
    """A finite number above zero, or None. A bool is not a price."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def _rounded(value) -> float | None:
    value = _positive(value)
    return round(value, 2) if value is not None else None


def _stamp(value) -> str | None:
    """The quote's own time, or None when it is missing or cannot be a time. Never a guess."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, clock.IST).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _fetch(url: str, timeout: float) -> bytes | None:
    """The body, or None when this host did not answer. A 429 waits BACKOFF and asks once more."""
    for attempt in (1, 2):
        request = urllib.request.Request(
            url, headers={"User-Agent": BROWSER, "Accept": "*/*", "Accept-Encoding": "gzip",
                          "Referer": "https://finance.yahoo.com/"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as err:
            try:
                err.close()
            except Exception:                                    # noqa: BLE001
                pass
            if err.code == 429 and attempt == 1:
                _time.sleep(BACKOFF)
                continue
            return None
        except (urllib.error.URLError, OSError, EOFError):
            return None
    return None


_EXCHANGE_FILE: dict = {}


def from_exchange(symbol: str) -> dict:
    """The exchange's own last traded price, from the daily file. A DAY OLD, and it says so."""
    import time
    from . import nse
    # One download per run, not one per stock: the file is the whole market, and the exchange
    # blocks after a few hundred requests a day. Kept ten minutes, so a long watch still
    # notices the next day's file.
    held = _EXCHANGE_FILE.get("file")
    if held and time.monotonic() - _EXCHANGE_FILE["got"] < 600:
        day, rows = held
    else:
        try:
            day, rows = nse.last_trading_day()
        except Exception as err:                                 # noqa: BLE001
            raise NoQuote(f"{symbol}: neither the intraday feed nor the exchange answered")                 from err
        _EXCHANGE_FILE.update(file=(day, rows), got=time.monotonic())
    for row in rows:
        if (row.get("symbol") or "").upper() == symbol.upper():
            close = _number(row.get("close"))
            if close is None:
                raise NoQuote(f"{symbol.upper()}: the exchange's file for {day} has no usable "
                              f"close ({row.get('close')!r})")
            previous = _number(row.get("previous_close"))
            return {"symbol": symbol.upper(), "price": round(close, 2),
                    "previous_close": round(previous, 2) if previous is not None else None,
                    "at": f"{day}T15:30:00+05:30", "currency": "INR",
                    "source": f"the exchange's closing file for {day}, not a live price"}
    if not any(isinstance(row, dict) and "symbol" in row for row in rows):
        # The exchange's second file format (UDiFF) comes back with its own column names. Saying
        # "SBIN is not in the file" then would be false: the file was simply not read.
        raise NoQuote(f"{symbol.upper()}: the exchange's file for {day} came in a shape this does "
                      f"not read (no symbol column), so no price can be taken from it")
    raise NoQuote(f"{symbol}: not in the exchange's file for {day}")


def _number(value) -> float | None:
    """A positive finite number from the exchange's file, which may carry it as text."""
    if isinstance(value, str):
        try:
            value = float(value.replace(",", ""))
        except ValueError:
            return None
    return _positive(value)


def quotes_for(symbols: list[str], source: str, offline: str | None,
               pause: float = PAUSE) -> tuple[dict, list[str]]:
    """Quotes for several symbols, one at a time, half a second apart. Errors are kept, not hidden."""
    got, errors = {}, []
    for number, symbol in enumerate(symbols):
        try:
            got[symbol] = quote(symbol, offline=offline, source=source)
        except (NoQuote, OSError) as err:
            errors.append(str(err))
        if not offline and number < len(symbols) - 1:
            _time.sleep(pause)
    return got, errors
