"""Company fundamentals from Upstox, and the five lines of it that lie.

Optional. Everything else in this pack works without a token; this is the one place one is needed,
because the exchange stopped publishing structured financial statements in December 2024 -- its
annual feed stops at March 2024 and its quarterly feed at December 2024, identically for every
company. Those are facts about the endpoint, not about any filer.

Get a token free at upstox.com/developer and put it in config.json as upstox_access_token, or in
the UPSTOX_ACCESS_TOKEN environment variable. Without one, this module says so and nothing crashes.

Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.upstox.com/v2/fundamentals"
CANDLES = "https://api.upstox.com/v3/historical-candle"
CRORE = 1e7

# Published: 50 requests a second, 500 a minute, 2,000 per THIRTY MINUTES. Only the last one binds,
# and that is easy to get wrong -- it averages 66 a minute, so a client can obey the per-second and
# per-minute limits and still be cut off. A run at 210 a minute went clean for 150 companies and
# then answered 429 on everything.
#
# So the window is kept rather than approximated, and the budget is set well under half of what is
# published. A refusal still counts against the allowance, so arguing with one lengthens it.
REQUESTS_PER_SECOND = 1.0
WINDOW_SECONDS = 30 * 60
WINDOW_BUDGET = 900
BACKOFF_ON_REFUSAL = 600

_pace = threading.Lock()
_free_at = 0.0
_window: list[float] = []
_quiet_until = 0.0


class NoToken(Exception):
    """No token configured. Recoverable: the caller should say so and carry on."""


class Refused(Exception):
    """The source would not answer. Temporary, and not a fact about the company."""


def token(explicit: str | None = None) -> str:
    """From the argument, then config.json, then the environment. Never from a committed file."""
    if explicit:
        return explicit
    for place in (Path.cwd() / "config.json",
                  Path(__file__).resolve().parent.parent / "config.json"):
        if place.exists():
            try:
                got = (json.loads(place.read_text(encoding="utf-8"))
                       .get("upstox_access_token") or "").strip()
            except (json.JSONDecodeError, OSError):
                got = ""
            if got:
                return got
    got = (os.environ.get("UPSTOX_ACCESS_TOKEN") or "").strip()
    if got:
        return got
    raise NoToken(
        "No Upstox token. Fundamentals and intraday candles need one; everything else in this pack "
        "does not. Get one free at upstox.com/developer, then put it in config.json as "
        "upstox_access_token or set UPSTOX_ACCESS_TOKEN.")


def _wait_turn() -> None:
    global _free_at
    gap = 1.0 / REQUESTS_PER_SECOND
    while True:
        with _pace:
            now = time.monotonic()
            cutoff = now - WINDOW_SECONDS
            while _window and _window[0] < cutoff:
                _window.pop(0)
            if now < _quiet_until:
                wait, spend = _quiet_until - now, False
            elif len(_window) >= WINDOW_BUDGET:
                wait, spend = _window[0] - cutoff + 0.1, False
            else:
                wait, spend = max(0.0, _free_at - now), True
                _free_at = max(now, _free_at) + gap
                _window.append(_free_at)
        if spend:
            if wait:
                time.sleep(wait)
            return
        time.sleep(min(wait, 60.0))


def _refused() -> None:
    global _quiet_until
    with _pace:
        _quiet_until = time.monotonic() + BACKOFF_ON_REFUSAL
        _window.clear()


# THIS SOURCE IS BEHIND CLOUDFLARE, AND IT REFUSES PYTHON.
#
# urllib.request gets 403 with Cloudflare error 1010, "access denied based on your browser's
# signature" -- a TLS fingerprint check that no header will satisfy, because the block is on how
# Python negotiates the connection rather than on what it claims to be. Setting a browser
# user-agent changes nothing.
#
# The curl binary gets 200 on the same URL with the same token, with or without a user-agent. So
# this shells out to curl, which ships with Windows 10 and later, macOS, and every Linux. The
# alternative was asking everyone to pip install a TLS-impersonation library for one optional
# skill, which is a worse trade.
#
# Measured 2026-10-24 against the balance-sheet, key-ratios and v3 candle endpoints.
def _curl(url: str, access: str, timeout: float) -> tuple[int, bytes]:
    import subprocess
    try:
        done = subprocess.run(
            ["curl", "-s", "-S", "--max-time", str(int(timeout)),
             "-w", "\n%{http_code}",
             "-H", f"Authorization: Bearer {access}",
             "-H", "Accept: application/json", url],
            capture_output=True, timeout=timeout + 10)
    except FileNotFoundError as err:
        raise Refused(
            "this source sits behind Cloudflare, which refuses Python's connection fingerprint "
            "(error 1010) however the request is dressed up. It answers the curl binary, which "
            "could not be found on this machine. Install curl, or skip the skills that need a "
            "token -- everything else in this pack works without one.") from err
    except subprocess.TimeoutExpired as err:
        raise Refused("the source did not answer in time") from err

    out = done.stdout or b""
    head, _, tail = out.rpartition(b"\n")
    try:
        status = int(tail.strip() or 0)
    except ValueError:
        status = 0
    return status, head


def fetch(url: str, access: str, params: dict | None = None, timeout: float = 45):
    _wait_turn()
    if params:
        url += "?" + urllib.parse.urlencode(params)
    status, body = _curl(url, access, timeout)

    if status == 429:
        _refused()
        raise Refused("the source is rate limiting; it allows 2,000 requests per 30 minutes and "
                      "recovers in about 9 minutes")
    if status == 401:
        raise Refused("the token was rejected. Upstox tokens expire daily, around 3:30am India "
                      "time, so a token that worked yesterday needs replacing")
    if status != 200:
        raise Refused(f"the source answered {status}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as err:
        raise Refused(f"the answer was not readable: {body[:80]!r}") from err


# ---- the five lines that lie ---------------------------------------------------------------------
# Measured against Reliance's audited accounts. Each of these would have produced a plausible wrong
# number rather than an obvious one, which is why they are named here rather than quietly mapped.
#
#   Equity Capital        total equity INCLUDING minority interest, not the owners' share. For
#                         Reliance 2026 it reads 10,85,866 against an audited 9,04,030: 20% out.
#   Profit After Tax      struck before the share of associates. 95,610 against 95,754.
#   Total Revenue         built on revenue net of excise, so not the total income the accounts show.
#   EPS - Basic/Diluted   mix two report vintages inside one series. Reliance 2023 to 2026 reads
#                         98.59, 51.45, 51.47, 59.69 -- the 2024 figure is the comparative restated
#                         after that October's bonus while 2023 is still as filed, so the series
#                         halves between them where nothing happened.
REFUSED_LINES = ("Equity Capital", "Profit After Tax", "Total Revenue",
                 "EPS - Basic", "EPS - Diluted")

BALANCE_SHEET = {"Total Assets": "assets"}
INCOME_STATEMENT = {"Revenue": "revenue", "Other Income": "other_income",
                    "Profit Before Tax": "profit_before_tax"}
CASH_FLOW = {"Cash flow from Operations": "cash_from_operations",
             "Cash flow from Investing": "cash_from_investing",
             "Cash flow from Financing": "cash_from_financing",
             "Cash (End of the year)": "cash"}
STATEMENTS = (("balance-sheet", BALANCE_SHEET), ("income-statement", INCOME_STATEMENT),
              ("cash-flow", CASH_FLOW))

MONTH_END = {"Mar": "03-31", "Jun": "06-30", "Sep": "09-30", "Dec": "12-31"}
PERIOD = re.compile(r"^([A-Z][a-z]{2})\s+(\d{4})$")


def period_end(raw: str) -> str | None:
    """'Mar 2026' becomes '2026-03-31'. Anything else becomes nothing rather than a guess."""
    m = PERIOD.match((raw or "").strip())
    if not m:
        return None
    tail = MONTH_END.get(m.group(1))
    return f"{m.group(2)}-{tail}" if tail else None


def figures(body: dict, mapping: dict, symbol: str) -> dict[str, dict[str, float]]:
    """Statement lines, by period, in rupees.

    Amounts arrive in crore. A unit this module has not seen is REFUSED rather than converted: a
    crore kept as a crore is a figure ten million times too small, and a guess at an unknown unit is
    how a plausible wrong number gets into a database.
    """
    data = (body or {}).get("data") or {}
    units = (data.get("units_in") or "").strip().lower()
    if units and units != "crore":
        raise Refused(f"{symbol}: amounts are in {units!r}, which this module does not read")
    out: dict[str, dict[str, float]] = {}
    for item in data.get("full_statement") or []:
        concept = mapping.get((item.get("particular") or "").strip())
        if concept is None:
            continue
        for entry in item.get("history") or []:
            when = period_end(entry.get("period"))
            value = entry.get("value")
            if when and value is not None:
                out.setdefault(when, {})[concept] = float(value) * CRORE
    return out


def ratios(body: dict) -> dict[str, dict[str, float]]:
    """The published ratios, as {name: {company, sector}}. A percent stays as written."""
    data = (body or {}).get("data")
    items = data if isinstance(data, list) else []
    if isinstance(data, dict):
        items = [v for value in data.values() if isinstance(value, list) for v in value]

    def number(raw):
        if raw is None:
            return None
        try:
            return float(str(raw).strip().replace(",", "").rstrip("%").strip())
        except ValueError:
            return None

    out = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        company, sector = number(item.get("company_value")), number(item.get("sector_value"))
        if name and (company is not None or sector is not None):
            out[name] = {k: v for k, v in (("company", company), ("sector", sector))
                         if v is not None}
    return out


def candles(isin: str, access: str, interval: str = "days/1",
            start: str = "2020-01-01", end: str | None = None) -> list[list]:
    """Historical candles. interval is 'days/1', 'minutes/1', 'minutes/5' and so on.

    Daily goes back to 2020; one-minute returns about 1,500 candles, roughly four trading days, so
    a year of one-minute data for one symbol costs about 63 requests. Across 2,647 symbols that is
    166,000 requests, which against a budget of 900 per half hour is days of collecting -- which is
    why intraday is for a watchlist and daily is for the market.
    """
    from datetime import date
    end = end or str(date.today())
    body = fetch(f"{CANDLES}/NSE_EQ|{isin}/{interval}/{end}/{start}", access)
    return ((body or {}).get("data") or {}).get("candles") or []
