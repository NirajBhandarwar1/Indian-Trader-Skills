"""The NSE clock: is the market open, when does it open next, and what day is it.

No key, no account. India is simpler than New York in one way and harder in another.

SIMPLER: India has no daylight saving. IST is UTC+5:30 all year, every year, so the offset is a
constant and no time zone database is needed. A tool written for New York has to work out whether
the clock has sprung forward; this one does not.

HARDER: the holiday list cannot be reasoned out. The NSE closes for Republic Day, Holi, Id-Ul-Fitr,
Mahavir Jayanti, Good Friday, Ambedkar Jayanti, Maharashtra Day, Bakri Id, Muharram, Independence
Day, Ganesh Chaturthi, Gandhi Jayanti, Dussehra, two days of Diwali, Guru Nanak Jayanti and
Christmas. Several of those move with the lunar calendar and are fixed by the exchange each year,
so the list is FETCHED FROM THE EXCHANGE rather than written in. The copy below is only a fallback
for when the network is unavailable, and it says which year it is good for -- a hard-coded list
that silently goes stale is how a tool tells you the market is open on Diwali.

Standard library only.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")

# The regular equity session. Pre-open runs 09:00 to 09:15 and is an auction, not continuous
# trading: an order there is matched at the open, so no entry is timed during it.
OPEN, CLOSE = time(9, 15), time(15, 30)
PRE_OPEN = time(9, 0)

HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
HOLIDAY_PAGE = "https://www.nseindia.com/resources/exchange-communication-holidays"

# Fetched from the exchange on 2026-10-24 and kept only for when the network is not there. A date
# marked with a star in the exchange's own list has a special session (see MUHURAT).
FALLBACK_YEAR = 2026
FALLBACK = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan*",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}

# MUHURAT TRADING is the Indian answer to a half day, and it is the opposite of one: the market is
# closed for Diwali Laxmi Pujan and then opens for about an hour in the evening for a ceremonial
# session. The exchange announces the hour a few weeks ahead and it is not the regular session, so
# this module reports the day as closed and says a special session may be announced. Guessing the
# hour would be worse than saying it is not known.
MUHURAT = "*"

_cache: dict[int, dict[str, str]] | None = None


class BadTime(ValueError):
    """A clock time that cannot be read. Said plainly by every skill, never a traceback."""


def now(pinned: str | None = None) -> datetime:
    """The current time in India, or a pinned one for replaying a day.

    `pinned` is "2026-10-23 10:15" in IST. Everything in this pack takes the clock from here so a
    past session can be replayed exactly.
    """
    if pinned:
        for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(pinned, shape).replace(tzinfo=IST)
            except ValueError:
                continue
        # An ISO time, as the US pack takes one: no offset is IST, "Z" or "+00:00" is converted.
        try:
            when = datetime.fromisoformat(str(pinned).strip().replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None:
            return (when.replace(tzinfo=IST) if when.tzinfo is None
                    else when.astimezone(IST))
        raise BadTime(f"--now wants a time like '2026-10-23 10:15' (IST), not {pinned!r}")
    return datetime.now(IST)


def holidays(year: int, offline: bool = False) -> dict[str, str]:
    """{'2026-10-20': 'Dussehra'} for the year, from the exchange where possible.

    Falls back to the copy above only for the year it was taken from. For any other year without
    the network this says so rather than claiming the market has no holidays, because an empty
    holiday list reads as "open every weekday", which would be wrong sixteen times a year.
    """
    global _cache
    if _cache is None:
        _cache = {}
    if year in _cache:
        return _cache[year]

    if not offline:
        try:
            from . import http
            body = http.fetch_json(HOLIDAY_URL, HOLIDAY_PAGE)
            found = {}
            for row in (body or {}).get("CM") or []:
                when = _iso(row.get("tradingDate") or "")
                if when and when.startswith(str(year)):
                    found[when] = (row.get("description") or "").strip()
            if found:
                _cache[year] = found
                return found
        except Exception:                                        # noqa: BLE001  fall through below
            pass

    if year == FALLBACK_YEAR:
        _cache[year] = dict(FALLBACK)
        return _cache[year]
    raise Unknown(f"the {year} NSE holiday list is not known here and the exchange could not be "
                  f"reached, so whether a given day is a trading day cannot be said")


class Unknown(Exception):
    """The holiday list for that year is not available. Never assumed to be empty."""


def _iso(raw: str) -> str | None:
    """'20-Oct-2026' to '2026-10-20'."""
    months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    parts = (raw or "").strip().split("-")
    if len(parts) != 3:
        return None
    month = months.get(parts[1][:3].title())
    try:
        return f"{int(parts[2]):04d}-{month:02d}-{int(parts[0]):02d}" if month else None
    except ValueError:
        return None


def holiday_name(day: date, offline: bool = False) -> str | None:
    """The holiday's name if the market is shut that day, else None."""
    return holidays(day.year, offline).get(day.isoformat())


def special_session(day: date, offline: bool = False) -> bool:
    """Whether the exchange marked this closed day as having a special session, like Muhurat."""
    return MUHURAT in (holiday_name(day, offline) or "")


def is_trading_day(day: date, offline: bool = False) -> bool:
    """Monday to Friday and not a holiday. NSE trades no weekends."""
    return day.weekday() < 5 and holiday_name(day, offline) is None


def is_open(at: datetime | None = None, offline: bool = False) -> bool:
    at = at or now()
    return is_trading_day(at.date(), offline) and OPEN <= at.time() < CLOSE


def status(at: datetime | None = None, offline: bool = False) -> str:
    """One line a person can read, never a guess about a special session's hours."""
    at = at or now()
    day = at.date()
    try:
        name = holiday_name(day, offline)
    except Unknown:
        # Said, not guessed, as the US pack says "check nyse.com for this date". A weekend needs no
        # holiday list to be called closed.
        if day.weekday() >= 5:
            return f"NSE CLOSED on {day:%a %Y-%m-%d}: the exchange does not trade at weekends."
        return (f"NSE status unknown for {day:%a %Y-%m-%d}: the {day.year} holiday list is not "
                f"known here. Check nseindia.com's holiday list for that date.")
    if name:
        extra = (" The exchange marked it for a special Muhurat session, whose hour it announces "
                 "separately and which is not the regular session." if MUHURAT in name else "")
        return f"NSE CLOSED on {day:%a %Y-%m-%d} for {name.rstrip(MUHURAT)}.{extra}"
    if day.weekday() >= 5:
        return f"NSE CLOSED on {day:%a %Y-%m-%d}: the exchange does not trade at weekends."
    if at.time() < PRE_OPEN:
        return f"NSE CLOSED. Pre-open starts 09:00 and the session opens 09:15 IST."
    if at.time() < OPEN:
        return (f"NSE PRE-OPEN ({at:%H:%M} IST). This is an auction, not continuous trading; the "
                f"session opens 09:15.")
    if at.time() < CLOSE:
        return f"NSE OPEN ({at:%Y-%m-%d %H:%M} IST, regular session, closes 15:30)."
    return f"NSE CLOSED ({at:%H:%M} IST). The session ran 09:15 to 15:30."


def next_open(at: datetime | None = None, offline: bool = False) -> datetime:
    at = at or now()
    day = at.date()
    if is_trading_day(day, offline) and at.time() < OPEN:
        return datetime.combine(day, OPEN, IST)
    for ahead in range(1, 30):
        nxt = day + timedelta(days=ahead)
        if is_trading_day(nxt, offline):
            return datetime.combine(nxt, OPEN, IST)
    raise Unknown("no trading day found in the next 30 days, which should not happen")


def previous_trading_day(before: date, offline: bool = False) -> date:
    for back in range(1, 30):
        day = before - timedelta(days=back)
        if is_trading_day(day, offline):
            return day
    raise Unknown("no trading day found in the previous 30 days")


# The tick size, MEASURED from a full day's traded prices rather than read off a circular. Of 2,706
# symbols on 2026-10-23: below 250 rupees most closes are not multiples of 5 paise (502 of 629
# under 50, and 645 of 900 between 50 and 250), and at 250 and above most are (707 of 744, and 421
# of 432 above 1,000). That is SEBI's 2024 reduction, and it matters because a stop rounded to the
# wrong tick is an order the exchange will not accept.
FINE_TICK_BELOW = 250.0
FINE_TICK, COARSE_TICK = 0.01, 0.05


def tick_size(price: float) -> float:
    return FINE_TICK if price < FINE_TICK_BELOW else COARSE_TICK


def to_tick(price: float, up: bool) -> float:
    """Round a price to a tick the exchange accepts, up or down as asked.

    A stop always rounds DOWN for a long and a target always rounds UP, so neither is ever moved
    closer to the entry than asked for. Rounding a stop towards the entry would quietly shrink the
    risk a share and buy more shares than the user's limit allows.
    """
    # The division is rounded before it is floored. 950.05 / 0.05 is 19000.999999999996 in
    # floating point, and flooring that put a stop sitting exactly on a tick a whole tick lower:
    # an opening-range low of 950.05 became a stop of 950.00. A price already on a tick stays put.
    tick = tick_size(price)
    steps = round(price / tick, 6)
    whole = math.ceil(steps) if up else math.floor(steps)
    return round(whole * tick, 2)


def to_json(value) -> str:
    return json.dumps(value, indent=1, sort_keys=True, default=str)
