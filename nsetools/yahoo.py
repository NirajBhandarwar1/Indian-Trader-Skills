"""Company accounts with no key, no account and no signup, for NSE stocks.

Indian symbols work here with a .NS suffix, and the endpoint needs no credential of any kind. Four
annual years come back per company, which is a hard cap: period1 set to 2010, 2000 or 1970 returns
the same four, on RELIANCE, TCS, HDFCBANK and ITC alike.

FOUR OF THE FIELD NAMES MEAN SOMETHING OTHER THAN THEY SAY, each measured against audited accounts
rather than read off the documentation. Two of the four are fixed here by reading the right field
and giving it a name that cannot be misread: the group's profit and the owners' profit are kept
apart, and the closing cash comes from the cash flow statement. The other two, the share count and
earnings per share, cannot be fixed by naming, because the figure itself is on a basis this source
does not state. Those two are checked before anything is published and refused when the check
fails -- a warning asks a reader to remember, a check refuses the number.

Standard library only.
"""
from __future__ import annotations

import gzip
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries"
REFERER = "https://finance.yahoo.com/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/122.0.0.0 Safari/537.36")

# Each entry is the field this source publishes and what it actually means. The comments are the
# point: four of these are named in a way that invites the wrong reading.
FIELDS = {
    "annualTotalRevenue": "revenue",
    "annualPretaxIncome": "profit_before_tax",
    "annualTotalAssets": "assets",
    "annualStockholdersEquity": "equity",
    "annualTotalDebt": "debt",
    "annualEBITDA": "ebitda",
    "annualReconciledDepreciation": "depreciation",
    "annualFreeCashFlow": "free_cash_flow",
    "annualOperatingCashFlow": "cash_from_operations",
    "annualInvestingCashFlow": "cash_from_investing",
    "annualFinancingCashFlow": "cash_from_financing",

    # TRAP 1. annualNetIncome is profit attributable to OWNERS, not the whole group's. For Reliance
    # 2026 it is 80,775 crore against a group profit of 95,754. Market value is a claim on the
    # owners' share, so this is the right figure for a price-to-earnings ratio -- but calling it
    # "net income" and comparing it with another source's group profit manufactures a 15%
    # disagreement that was never a disagreement.
    "annualNetIncome": "net_profit_owners",
    "annualNetIncomeIncludingNoncontrollingInterests": "net_profit",

    # TRAP 2. annualCashAndCashEquivalents reads like the cash position and is only part of it.
    # Against Reliance's audited balance sheets for 2023 to 2026 the closing cash from the cash
    # flow statement is exact all four years -- 68,664 / 97,225 / 106,502 / 145,977 -- while the
    # field that sounds right gives 34,156 / 93,540 / 100,645 / 137,327, out by 3.8% to 50.3%.
    "annualEndCashPosition": "cash",

    # TRAP 3, AND IT IS WHY EARNINGS PER SHARE IS NOT READ AT ALL. annualBasicEPS and
    # annualDilutedEPS are RESTATED to the current share basis, which is not the figure the company
    # filed at the time: Reliance FY2023 reads 49.295 where the annual report says 98.59, exactly
    # twice, because the later 1:1 bonus doubled the count and halved every per-share figure before
    # it. Both are right on their own basis and neither says which basis it is on.
    #
    # There is no second source for it in this pack, so the only available check is arithmetic:
    # profit divided by the share count should reproduce it. That check cannot be exact, because
    # the published figure is already rounded -- Reliance FY2023 derives 49.2910 against a
    # published 49.295, 0.008% apart, which is the rounding in an as-filed 98.59 carried through a
    # halving. Publishing it would mean accepting a tolerance on a figure nobody can confirm, so
    # these two fields are deliberately not mapped. A per-share figure the reader can trust needs
    # an as-filed EPS from the annual report, which no free source carries.

    # TRAP 4 is annualOrdinarySharesNumber, and it is NOT IN THIS MAP ON PURPOSE. See
    # UNCONFIRMED_SHARES below: it is fetched separately, by a function whose name says it is
    # unconfirmed, so that it cannot end up inside a set of accounts by accident.
}

# The share count, kept out of FIELDS so that accounts() CANNOT RETURN IT. Filtering it out when
# printing would not be enough: whatever accounts() returns is what --save writes to results.json
# and what nse-scan then reads, so a figure that is merely hidden on screen is still a figure that
# travels into another skill's input. Keeping it out of the accounts means the stored data has
# nothing unproved in it.
#
# What it is: the filed count restated to the current share basis. It is NOT frozen at today's
# count -- TCS reads 3,659,051,373 for FY2023 and 3,618,087,518 after, which is its buyback -- so
# it does carry real per-year movement. Two things still make it unusable on its own:
#
#   1. The basis. Reliance reads 1,353 crore against FY2023, where the company filed 676.6 crore,
#      because of the 1:1 bonus that went ex 2024-10-28.
#   2. What is counted. This is shares ISSUED. The exchange's shareholding pattern counts shares
#      HELD BY SHAREHOLDERS, which excludes stock a company holds in trust for itself. Identical
#      for TCS and SUNPHARMA, 1.78% apart for Reliance at March 2026, 2.00% at March 2025 and
#      13.36% for HDFCBANK. The gap drifts year to year, so no fixed adjustment recovers one from
#      the other.
#
# So it is read only to be checked against the exchange's own filing, and only a count the two
# report identically ever becomes data. See shares.confirmed().
UNCONFIRMED_SHARES = "annualOrdinarySharesNumber"

# Concepts that must be proved against a second source before they become data at all.
VERIFIED_AGAINST = {
    "shares": "the exchange's own shareholding filing for the same date, which must match exactly",
}

RUPEES = "INR"


class NotReported(Exception):
    """This source has no accounts for the symbol. For a fund or a trust that is the right answer."""


class Unreachable(NotReported):
    """The source could not be asked, or its answer could not be read. Not the same as "it has
    none": a caller that says a company reports nothing must not say it about this."""


def _fetch(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate", "Referer": REFERER})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body
    except urllib.error.HTTPError as err:
        raise Unreachable(f"the source answered {err.code}") from err
    except (urllib.error.URLError, OSError) as err:
        raise Unreachable(f"could not reach the source: {getattr(err, 'reason', err)}") from err


def _series(symbol: str, wanted: dict[str, str], timeout: float = 30) -> dict[str, dict]:
    """One request for the given fields, by period end, in rupees.

    `wanted` maps the source's field name to the name this pack uses, and NOTHING outside it is
    read. That is what keeps an unproved figure out of a set of accounts: a caller that did not ask
    for the share count cannot receive one.

    The currency is carried through rather than assumed. One source reports Infosys in US dollars,
    and against a rupee share price that is wrong by about eighty times with nothing in the numbers
    to show it, so a figure not in rupees is refused rather than converted.
    """
    ticker = urllib.parse.quote(f"{symbol.upper()}.NS", safe="")      # M&M is a real symbol
    url = (f"{BASE}/{ticker}?symbol={ticker}&type={','.join(wanted)}"
           f"&period1=0&period2={int(time.time())}&merge=false")
    try:
        parsed = json.loads(_fetch(url, timeout))
    except (ValueError, OSError, EOFError) as err:     # bad JSON, bad UTF-8, a broken gzip body
        raise Unreachable(f"{symbol}: the answer was not readable") from err

    series_of = parsed.get("timeseries") if isinstance(parsed, dict) else None
    blocks = (series_of.get("result") if isinstance(series_of, dict) else None) or []
    by_year: dict[str, dict] = {}
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        for key, series in block.items():
            if key in ("meta", "timestamp"):
                continue
            concept = wanted.get(key)
            if concept is None:
                continue
            for entry in series or []:
                if not isinstance(entry, dict):
                    continue
                when = entry.get("asOfDate")
                reported = entry.get("reportedValue") or {}
                value = reported.get("raw")
                try:
                    value = None if isinstance(value, bool) else float(value)
                except (TypeError, ValueError):
                    value = None
                # nan or inf is not a figure; left blank rather than carried as one.
                if not when or value is None or not math.isfinite(value):
                    continue
                currency = (entry.get("currencyCode") or RUPEES).upper()
                year = by_year.setdefault(when, {"currency": currency, "values": {}})
                if currency != RUPEES:
                    year["currency"] = currency
                year["values"][concept] = float(value)

    if not by_year:
        raise NotReported(f"{symbol}: nothing reported")
    return by_year


def accounts(symbol: str, timeout: float = 30) -> dict[str, dict]:
    """Four years of accounts for one NSE symbol, by period end, in rupees.

    Carries no share count and no per-share figure, because neither can be confirmed from this
    source alone. Everything in here is a figure this source reports and nothing is derived, so
    what a caller stores is what was published.
    """
    return _series(symbol, FIELDS, timeout)


def unconfirmed_share_counts(symbol: str, timeout: float = 30) -> dict[str, float]:
    """The share count this source reports, by period end. UNCONFIRMED, and named so.

    For checking against the exchange's own filing and nothing else. Do not store what this
    returns and do not show it: on its own it cannot be told apart from a count on a different
    basis. shares.confirmed() is the function that turns it into data, and only where a second
    source reports the identical number.
    """
    try:
        raw = _series(symbol, {UNCONFIRMED_SHARES: "shares"}, timeout)
    except Unreachable:
        raise                     # "could not ask" is said by the caller, never read as "none"
    except NotReported:
        return {}
    return {when: year["values"]["shares"] for when, year in raw.items()
            if "shares" in year["values"]}


def in_rupees(year: dict) -> bool:
    return (year.get("currency") or RUPEES).upper() == RUPEES
