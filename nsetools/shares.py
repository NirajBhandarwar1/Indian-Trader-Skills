"""The share count as the company filed it, from the exchange's own shareholding XBRL.

No key, no account. This is a genuine SECOND SOURCE for a figure that otherwise has only one, and
it is the reason this pack can publish a share count at all: one source for a share count is a
number nobody can check.

WHY A SECOND SOURCE WAS NEEDED. The accounts source reports shares ISSUED. The exchange's
shareholding pattern reports shares HELD BY SHAREHOLDERS, which excludes stock held in trust for
the company itself. For a company with no such holding the two are identical to the share --
measured exactly equal for TCS and SUNPHARMA, and 0.02% apart for ITC. For a company that does
hold its own stock they are not, and the gap drifts year to year: Reliance reads 1.78% apart at
March 2026 and 2.00% at March 2025, and HDFCBANK 13.36%. Neither source is wrong; they are
counting different things, and nothing free says which belongs in a given per-share calculation.

So the rule is agreement, not preference. Where the two agree the count is published; where they
disagree it is refused along with every per-share figure built on it.

Standard library only.
"""
from __future__ import annotations

import re

from . import http, nse, yahoo
from .actions import MONTHS

# The grand total across every shareholder category. Taken as the largest value of this fact
# because the context that carries the total was RENAMED between taxonomy versions --
# "ShareholdingPatternI" in the 2022 schema, "ShareholdingPattern_ContextI" in the 2025 one. A
# parser keyed on either name returns nothing at all for half the filings and reads as "the
# exchange has no share count", which is the exact kind of silent wrong answer this pack exists to
# avoid. The total is the largest by construction: every other context is a subset of it.
TOTAL = re.compile(r"<[\w.-]+:NumberOfFullyPaidUpEquityShares\s+contextRef=\"[^\"]*\"[^>]*>"
                   r"\s*(\d+)\s*<")


class NotFiled(Exception):
    """No shareholding filing for that date. Common for a recent listing."""


def _iso(raw: str) -> str | None:
    """'31-MAR-2023' to '2023-03-31'. Anything else becomes nothing rather than a guess."""
    parts = (raw or "").strip().split("-")
    if len(parts) != 3:
        return None
    month = MONTHS.get(parts[1][:3].upper())
    try:
        return f"{int(parts[2]):04d}-{month:02d}-{int(parts[0]):02d}" if month else None
    except ValueError:
        return None


def filings(symbol: str) -> dict[str, str]:
    """Every shareholding filing for the symbol, as {period end: XBRL url}, newest kept.

    Dates are compared as dates rather than strings. Sorting these as text puts 31-MAR before
    30-SEP in the same year, which is how a "latest filing" turns out to be eighteen months old.
    """
    out: dict[str, str] = {}
    for row in nse.shareholding(symbol):
        when, url = _iso(row.get("date") or ""), (row.get("xbrl") or "").strip()
        if when and url and when not in out:
            out[when] = url
    return out


class Counts(dict):
    """{period end: count}, with `unreadable`: {period end: why} for filings that exist but could
    not be read. Kept apart because "filed none" and "could not read it" are different answers."""

    def __init__(self):
        super().__init__()
        self.unreadable: dict[str, str] = {}


def as_filed(symbol: str, wanted: list[str]) -> dict[str, int]:
    """The filed total share count for each period end in `wanted`.

    One request per period, and only for periods asked about, because each filing is around half a
    megabyte. A period with no filing is left out rather than filled in from a neighbouring
    quarter: a count from December is not the count from March. A filing that is listed but could
    not be fetched, or carries no total, is left out too and its reason kept in `.unreadable`.
    """
    available = filings(symbol)
    out = Counts()
    for when in wanted:
        url = available.get(when)
        if not url:
            continue
        try:
            body = http.fetch(url).decode("utf-8", "replace")
        except Exception as err:                             # noqa: BLE001  unreachable, not absent
            out.unreadable[when] = f"its shareholding filing could not be fetched ({err})"
            continue
        found = [int(v) for v in TOTAL.findall(body)]
        if found:
            out[when] = max(found)
        else:
            out.unreadable[when] = "its shareholding filing carries no total share count"
    return out


def confirmed(symbol: str, wanted: list[str]) -> tuple[dict[str, int], dict[str, str]]:
    """Share counts two independent sources report identically, and why the rest are missing.

    Returns ({period end: count}, {period end: why not}). THE FIRST OF THOSE IS THE ONLY SHARE
    COUNT THIS PACK TREATS AS DATA. Nothing enters it that the accounts source and the exchange's
    own shareholding filing did not report as the same number, to the share.

    A count that is not in it is absent, not zero and not approximate, and the reason is carried
    beside it so a caller can say why rather than leaving a silent blank.
    """
    counts, missing = {}, {}
    accounts_down = exchange_down = None
    try:
        accounts = yahoo.unconfirmed_share_counts(symbol)
    except Exception as err:                                     # noqa: BLE001
        accounts, accounts_down = {}, f"the accounts source could not be read ({err})"
    try:
        exchange = as_filed(symbol, wanted)
    except Exception as err:                                     # noqa: BLE001
        exchange = {}
        exchange_down = f"the exchange's shareholding filings could not be read ({err})"
    exchange_unreadable = getattr(exchange, "unreadable", {}) or {}

    for when in wanted:
        mine, theirs = accounts.get(when), exchange.get(when)
        # A source that could not be read is said as that. Saying it "reports none" or "filed
        # none" when it was never reached would be a false statement about the company.
        theirs_why = exchange_down or exchange_unreadable.get(when)
        if mine is None and accounts_down:
            missing[when] = (f"{accounts_down}, so the count has nothing to confirm it"
                             + (f"; {theirs_why}" if theirs_why else ""))
        elif theirs is None and theirs_why:
            missing[when] = f"{theirs_why}, so the count has nothing to confirm it"
        elif mine is None and theirs is None:
            missing[when] = "neither source reports a share count for this date"
        elif mine is None:
            missing[when] = "only the exchange reports a count, with nothing to confirm it"
        elif theirs is None:
            missing[when] = ("the exchange filed no shareholding pattern for this date, so the "
                             "count has one source and nothing to confirm it")
        elif agree(mine, theirs):
            counts[when] = int(mine)
        else:
            missing[when] = "the two sources do not report the same count"
    return counts, missing


def agree(mine: float | None, theirs: int | None) -> bool:
    """Whether two independent share counts are the SAME NUMBER. Exactly, to the share.

    There is no tolerance here on purpose. A share count is a count of things, so two sources
    either report the same one or they report different ones, and a percentage band would only
    decide how much of a difference to call nothing. ITC's two counts sit 0.02% apart, which reads
    like rounding and is 3,104,065 actual shares.

    Measured across eight companies at March 2026: TCS and SUNPHARMA identical to the share, and
    ITC, RELIANCE, WIPRO, LT and HDFCBANK not. The identical ones are published and the rest are
    withheld.
    """
    if mine is None or theirs is None:
        return False
    return float(mine).is_integer() and int(mine) == int(theirs)
