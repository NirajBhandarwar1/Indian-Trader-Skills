"""Company accounts with no key, no account and no signup, for NSE stocks.

Indian symbols work here with a .NS suffix, and the endpoint needs no credential of any kind. Four
annual years come back per company, which is a hard cap: period1 set to 2010, 2000 or 1970 returns
the same four, on RELIANCE, TCS, HDFCBANK and ITC alike.

THE FIELD NAMES LIE IN FOUR PLACES, and each one was measured against audited accounts rather than
read off the documentation. Those four notes are the most valuable thing in this file.

Standard library only.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
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

    # TRAP 3. annualBasicEPS is restated for later bonuses and splits, which is useful and is NOT
    # the figure a company filed at the time. Comparing it against an as-filed figure from anywhere
    # else puts the two exactly a bonus factor apart and looks like one of them being wrong.
    "annualBasicEPS": "eps_basic",
    "annualDilutedEPS": "eps_diluted",

    # TRAP 4, and the worst. annualOrdinarySharesNumber is the share count TODAY, not the count at
    # that period end. Reliance's 1:1 bonus went ex on 2024-10-28; this field reports 1,353 crore
    # shares against both FY2023 and FY2024, which closed before it, where the real count was 676.6
    # crore. Any per-share figure built from it for a year before the latest bonus or split is
    # wrong by the bonus factor. Use it for today and never for history.
    "annualOrdinarySharesNumber": "shares",
}

# Which fields carry that warning into the output, so a reader cannot take them at face value.
WARNED = {
    "shares": "today's share count, not the count at that year end",
    "eps_basic": "restated for later bonuses, so not as filed",
    "eps_diluted": "restated for later bonuses, so not as filed",
    "net_profit_owners": "owners' share, not the whole group's",
}

RUPEES = "INR"


class NotReported(Exception):
    """This source has no accounts for the symbol. For a fund or a trust that is the right answer."""


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
        raise NotReported(f"the source answered {err.code}") from err
    except urllib.error.URLError as err:
        raise NotReported(f"could not reach the source: {err.reason}") from err


def accounts(symbol: str, timeout: float = 30) -> dict[str, dict]:
    """Four years for one NSE symbol, by period end, in rupees.

    The currency is carried through rather than assumed. One source reports Infosys in US dollars,
    and against a rupee share price that is wrong by about eighty times with nothing in the numbers
    to show it, so a figure not in rupees is refused rather than converted.
    """
    ticker = f"{symbol.upper()}.NS"
    url = (f"{BASE}/{ticker}?symbol={ticker}&type={','.join(FIELDS)}"
           f"&period1=0&period2={int(time.time())}&merge=false")
    try:
        parsed = json.loads(_fetch(url, timeout))
    except json.JSONDecodeError as err:
        raise NotReported(f"{symbol}: the answer was not readable") from err

    blocks = ((parsed or {}).get("timeseries") or {}).get("result") or []
    by_year: dict[str, dict] = {}
    for block in blocks:
        for key, series in block.items():
            if key in ("meta", "timestamp"):
                continue
            concept = FIELDS.get(key)
            if concept is None:
                continue
            for entry in series or []:
                if not isinstance(entry, dict):
                    continue
                when = entry.get("asOfDate")
                reported = entry.get("reportedValue") or {}
                value = reported.get("raw")
                if not when or value is None:
                    continue
                currency = (entry.get("currencyCode") or RUPEES).upper()
                year = by_year.setdefault(when, {"currency": currency, "values": {}})
                if currency != RUPEES:
                    year["currency"] = currency
                year["values"][concept] = float(value)

    if not by_year:
        raise NotReported(f"{symbol}: no accounts reported")
    return by_year


def in_rupees(year: dict) -> bool:
    return (year.get("currency") or RUPEES).upper() == RUPEES
