"""What the exchange publishes, and the traps in each one.

Every endpoint here was checked against live answers rather than documentation. The notes are the
valuable part: each one is a way the data looks right and is not.
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, timedelta

from .http import ARCHIVES, HOME, NotPublished, fetch, fetch_json

EQUITY_LIST = f"{ARCHIVES}/content/equities/EQUITY_L.csv"
ANNOUNCEMENTS = f"{HOME}/api/corporate-announcements"
SHAREHOLDING = f"{HOME}/api/corporate-share-holdings-master"
ACTIONS = f"{HOME}/api/corporates-corporateActions"
INSIDER = f"{HOME}/api/corporates-pit"
DEALS = f"{HOME}/api/historicalOR/bulk-block-short-deals"
FLOWS = f"{HOME}/api/fiidiiTradeReact"
SECTORS = f"{ARCHIVES}/content/indices/ind_niftytotalmarket_list.csv"

# The page a browser would be on. The exchange checks it.
PAGES = {
    "announcements": f"{HOME}/companies-listing/corporate-filings-announcements",
    "shareholding": f"{HOME}/companies-listing/corporate-filings-shareholding-pattern",
    "actions": f"{HOME}/companies-listing/corporate-filings-actions",
    "insider": f"{HOME}/companies-listing/corporate-filings-insider-trading",
    "deals": f"{HOME}/report-detail/display-bulk-and-block-deals",
    "flows": f"{HOME}/reports/fii-dii",
}

# Delivery data lives only in the full bhavcopy, and only this file has it. The newer per-day file
# carries prices without it, which is why both are tried in order.
BHAVCOPY_FULL = f"{ARCHIVES}/products/content/sec_bhavdata_full_%d%m%Y.csv"
BHAVCOPY_UDIFF = f"{ARCHIVES}/content/cm/BhavCopy_NSE_CM_0_0_0_%Y%m%d_F_0000.csv.zip"


def equities() -> list[dict]:
    """Every listed equity: symbol, name, ISIN, series, listing date, face value."""
    rows = list(csv.DictReader(io.StringIO(fetch(EQUITY_LIST).decode("utf-8", "replace"))))
    out = []
    for row in rows:
        clean = {k.strip().lower().replace(" ", "_"): (v or "").strip() for k, v in row.items()}
        if clean.get("series") != "EQ":
            continue
        # The file calls it ISIN NUMBER and everything else calls it isin, including the broker API
        # that needs it to identify an instrument. Aliased rather than renamed so the original
        # column is still there for anyone comparing against the file.
        clean["isin"] = clean.get("isin_number", "")
        clean["name"] = clean.get("name_of_company", "")
        out.append(clean)
    return out


_equity_symbols: set[str] | None = None


def equity_symbols() -> set[str]:
    """Just the symbols that are companies, cached for the process.

    The daily bhavcopy carries about 2,706 symbols where the equity master list carries 2,359, and
    the difference is almost entirely exchange traded funds. They matter because a fund is held
    rather than traded, so it sits near 100% delivery permanently: screen the bhavcopy for high
    delivery without this filter and the whole first page is LIQUIDBETF, BANKIETF and MOMENTUM30
    rather than a single company. Measured on 2026-10-23.
    """
    global _equity_symbols
    if _equity_symbols is None:
        _equity_symbols = {row["symbol"].upper() for row in equities() if row.get("symbol")}
    return _equity_symbols


def companies_only(rows: list[dict]) -> list[dict]:
    """Drop the funds from a day's rows, keeping the companies."""
    keep = equity_symbols()
    return [r for r in rows if (r.get("symbol") or "").upper() in keep]


def announcements(symbol: str | None = None, days: int = 7) -> list[dict]:
    """Corporate announcements, which is where a result, an order win or a resignation shows up.

    This is the Indian equivalent of an 8-K and it is the fastest public signal the exchange carries.
    """
    to_day = date.today()
    from_day = to_day - timedelta(days=days)
    url = (f"{ANNOUNCEMENTS}?index=equities&from_date={from_day:%d-%m-%Y}"
           f"&to_date={to_day:%d-%m-%Y}")
    if symbol:
        url += f"&symbol={symbol.upper()}"
    rows = fetch_json(url, PAGES["announcements"])
    return rows if isinstance(rows, list) else []


def shareholding(symbol: str) -> list[dict]:
    """Quarterly shareholding filings, newest first. Each carries its own XBRL document.

    A WARNING worth keeping: the percentages changed convention mid-series. Up to the June 2025
    quarter they are written as percentages (54.48); from September 2025 as fractions (0.5448),
    exchange-wide for every company. Reading every quarter the same way puts a promoter holding of
    5,448% on screen. Work the convention out per filing -- promoters plus public own the whole
    company, so they sum to 100 one way and 1 the other.
    """
    rows = fetch_json(f"{SHAREHOLDING}?index=equities&symbol={symbol.upper()}",
                      PAGES["shareholding"])
    return rows if isinstance(rows, list) else []


def corporate_actions(symbol: str | None = None, since: str = "01-01-2015") -> list[dict]:
    """Dividends, bonus issues, splits and rights, by ex-date.

    Needed to read any price history honestly: a 1:1 bonus halves the price overnight and a chart
    that does not know about it shows a 50% crash. Goes back further than most free sources.
    """
    today = date.today().strftime("%d-%m-%Y")
    url = f"{ACTIONS}?index=equities&from_date={since}&to_date={today}"
    if symbol:
        url += f"&symbol={symbol.upper()}"
    rows = fetch_json(url, PAGES["actions"])
    return rows if isinstance(rows, list) else []


def insider_trades(symbol: str | None = None, days: int = 30) -> list[dict]:
    """Insider and promoter dealings filed under the PIT regulations.

    The Indian answer to watching congressional disclosures, and a better one: these are filed
    within two trading days of the transaction rather than up to 45 days later.

    A SYMBOL IS REQUIRED IN PRACTICE. Asked market-wide the endpoint answers 200 with an empty
    data list, however wide the date range -- measured over 90 days. That reads as "no insider
    activity anywhere", which is wrong: ask for INFY over a year and 197 filings come back, and
    RELIANCE 4. So iterate over the symbols you care about rather than trusting one broad call.
    """
    to_day = date.today()
    from_day = to_day - timedelta(days=days)
    url = (f"{INSIDER}?index=equities&from_date={from_day:%d-%m-%Y}&to_date={to_day:%d-%m-%Y}")
    if symbol:
        url += f"&symbol={symbol.upper()}"
    rows = fetch_json(url, PAGES["insider"])
    if isinstance(rows, dict):
        rows = rows.get("data") or []
    return rows if isinstance(rows, list) else []


def bulk_block_deals(kind: str = "bulk_deals", days: int = 7) -> list[dict]:
    """Bulk and block deals, and short selling. kind is bulk_deals, block_deals or short_selling.

    A bulk deal is more than 0.5% of a company's shares in one day; a block deal is a single
    negotiated trade above 10 crore. Both name the buyer and seller, which an ordinary tape does not.
    """
    to_day = date.today()
    from_day = to_day - timedelta(days=days)
    url = (f"{DEALS}?optionType={kind}&from={from_day:%d-%m-%Y}&to={to_day:%d-%m-%Y}")
    rows = fetch_json(url, PAGES["deals"])
    if isinstance(rows, dict):
        rows = rows.get("data") or []
    return rows if isinstance(rows, list) else []


def fii_dii_flows() -> list[dict]:
    """What foreign and domestic institutions bought and sold, in crore, for the latest day."""
    rows = fetch_json(FLOWS, PAGES["flows"])
    return rows if isinstance(rows, list) else []


def sectors() -> dict[str, str]:
    """Which industry a company belongs to, from the broadest index's membership file.

    Only about 755 companies are classified anywhere free. Everything below that is unclassified,
    and no amount of looking changes it.
    """
    text = fetch(SECTORS).decode("utf-8", "replace")
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        clean = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
        symbol, industry = clean.get("symbol"), clean.get("industry")
        if symbol and industry:
            out[symbol.upper()] = industry
    return out


def _parse_full_bhavcopy(text: str) -> list[dict]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        clean = {k.strip().upper().replace(" ", "_"): (v or "").strip() for k, v in row.items()}
        if clean.get("SERIES") != "EQ":
            continue

        def number(key):
            raw = clean.get(key, "")
            if raw in ("", "-", "NA"):
                return None
            try:
                return float(raw.replace(",", ""))
            except ValueError:
                return None

        out.append({
            "symbol": clean.get("SYMBOL", ""),
            "date": clean.get("DATE1", ""),
            "open": number("OPEN_PRICE"),
            "high": number("HIGH_PRICE"),
            "low": number("LOW_PRICE"),
            "close": number("CLOSE_PRICE"),
            "previous_close": number("PREV_CLOSE"),
            "volume": number("TTL_TRD_QNTY"),
            "turnover": number("TURNOVER_LACS"),
            "trades": number("NO_OF_TRADES"),
            "delivered": number("DELIV_QTY"),
            # The figure nothing outside India publishes: what share of the day's volume actually
            # changed hands rather than being bought and sold again before the close.
            "delivery_percent": number("DELIV_PER"),
        })
    return out


def bhavcopy(day: date | None = None) -> tuple[list[dict], str]:
    """One trading day for every stock, with delivery. Returns the rows and which file they came from.

    Delivery percentage is the reason to use this file rather than any price API. It separates real
    accumulation from intraday churn, and no free source outside India publishes anything like it.

    A weekend or a holiday has no file, which is the right answer rather than an error. The exchange
    also publishes late: asking for today before about 7pm India time usually finds nothing.
    """
    day = day or date.today()
    for template, name in ((BHAVCOPY_FULL, "full"), (BHAVCOPY_UDIFF, "udiff")):
        url = day.strftime(template)
        try:
            body = fetch(url)
        except NotPublished:
            continue
        if name == "full":
            return _parse_full_bhavcopy(body.decode("utf-8", "replace")), "full"
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            inner = zf.read(zf.namelist()[0]).decode("utf-8", "replace")
        rows = [r for r in csv.DictReader(io.StringIO(inner))]
        return rows, "udiff"
    raise NotPublished(f"no bhavcopy for {day:%Y-%m-%d}; a weekend, a holiday, or not published yet")


def last_trading_day(before: date | None = None, tries: int = 8) -> tuple[date, list[dict]]:
    """The most recent day the exchange actually published, with its rows."""
    day = before or date.today()
    for _ in range(tries):
        if day.weekday() < 5:
            try:
                rows, _which = bhavcopy(day)
                if rows:
                    return day, rows
            except NotPublished:
                pass
        day -= timedelta(days=1)
    raise NotPublished("no published trading day found in the last eight attempts")
