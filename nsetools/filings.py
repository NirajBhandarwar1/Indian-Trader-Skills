"""A company's quarterly results as the company filed them with the exchange. No key, no account.

This is the Indian equivalent of a 10-Q in XBRL. Every listed company files each quarter's results
with NSE as machine-readable XBRL, and the exchange publishes the file, so the figures here are the
company's own -- not an aggregator's reading of them.

TWO LISTINGS, JOINED. Measured on 2026-10-24:

  * /api/integrated-filing-results ("Integrated Filing - Financials") holds every quarter from the
    one ending 31-Mar-2025, current to the day: TCS's September 2026 quarter was there within
    minutes of the board meeting on 8 October.
  * /api/corporates-financial-results, the older listing, holds quarters up to 31-Dec-2024 and
    nothing after. It looked frozen; the filings had moved to the first listing, which SEBI's
    integrated filing format replaced it with.

Together they are one unbroken series. Where both carry the same quarter the newer listing wins.

WHAT A FILING HOLDS, and the trap in it. A results filing carries the quarter AND the year to date
in one file, and in an annual filing both can carry dates inside the same three months (measured on
Reliance's 2024 annual filing in an earlier build). Only the context id separates them: OneD is the
quarter, FourD the year to date. So this reads OneD by id, then checks that its dates really span a
quarter, and refuses a file where they do not. Across 146 filings of 7 companies, 2024 to 2026,
every taxonomy version, OneD was a clean three months every time, with no segment breakdown.

ELEMENT NAMES ARE MATCHED EXACTLY. ProfitLossForPeriod is a prefix of
ProfitLossForPeriodFromContinuingOperations, and a prefix match reads the wrong one. Banks file under
a different taxonomy entirely (InterestEarned, ProfitLossForThePeriod -- note "ForThe"), and a map
built for an ordinary company makes every bank look as if it filed nothing.

Standard library only.
"""
from __future__ import annotations

import gzip
import json
import math
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

from urllib.parse import quote

from .http import HOME, NotPublished, fetch, fetch_json

INTEGRATED = (f"{HOME}/api/integrated-filing-results?index=equities&symbol={{symbol}}"
              f"&period_ended=all&type=Integrated%20Filing-%20Financials")
LEGACY = f"{HOME}/api/corporates-financial-results?index=equities&symbol={{symbol}}&period=Quarterly"
BOARD_MEETINGS = f"{HOME}/api/corporate-board-meetings?index=equities&symbol={{symbol}}"
PAGES = {
    "integrated": f"{HOME}/companies-listing/corporate-integrated-filing",
    "legacy": f"{HOME}/companies-listing/corporate-filings-financial-results",
    "meetings": f"{HOME}/companies-listing/corporate-filings-board-meetings",
}

XBRLI = "{http://www.xbrl.org/2003/instance}"
QUARTER = "OneD"
YEAR_TO_DATE = "FourD"

# An ordinary company (Ind AS) and a bank file different statements. Each name maps to the element
# the company files, matched exactly.
INDAS = {
    "revenue": "RevenueFromOperations",
    "other_income": "OtherIncome",
    "total_income": "Income",
    "finance_costs": "FinanceCosts",
    "materials": "CostOfMaterialsConsumed",
    "purchases": "PurchasesOfStockInTrade",
    "inventory_change": "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade",
    "profit_before_exceptional": "ProfitBeforeExceptionalItemsAndTax",
    "exceptional": "ExceptionalItemsBeforeTax",
    "pbt": "ProfitBeforeTax",
    "profit": "ProfitLossForPeriod",
    "profit_owners": "ProfitOrLossAttributableToOwnersOfParent",
    # Inside the profit above, and not the business that is left: ITC's March 2025 quarter carried
    # 14,652.61 crore of it, the hotels it demerged, against 19,727 crore of owners' profit.
    "discontinued": "ProfitLossFromDiscontinuedOperationsAfterTax",
    "eps_basic": "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "eps_diluted": "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
}
BANKING = {
    # A bank has no "revenue from operations". Its top line as filed is total income: interest
    # earned plus other income. That is what "revenue" means for a bank here, and it is said so.
    "revenue": "Income",
    "interest_earned": "InterestEarned",
    "interest_expended": "InterestExpended",
    "other_income": "OtherIncome",
    "operating_profit": "OperatingProfitBeforeProvisionAndContingencies",
    "provisions": "ProvisionsOtherThanTaxAndContingencies",
    "exceptional": "ExceptionalItems",
    "pbt": "ProfitLossFromOrdinaryActivitiesBeforeTax",
    "profit": "ProfitLossForThePeriod",
    "profit_owners": "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
    "eps_basic": "BasicEarningsPerShareAfterExtraordinaryItems",
    "eps_diluted": "DilutedEarningsPerShareAfterExtraordinaryItems",
}


class NotFiled(Exception):
    """Nothing usable on the exchange for this, and the reason. Never a reason to estimate."""


def _day(raw: str | None) -> date | None:
    """'30-SEP-2026', '31-Dec-2024' or '2026-09-30' to a date; anything else to nothing."""
    text = str(raw or "").strip()
    for shape in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.title() if "-" in text[:3] else text, shape).date()
        except ValueError:
            continue
    return None


def _stamp(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    for shape in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(text, shape).isoformat()
        except ValueError:
            continue
    return None


def _saved(offline: str, name: str):
    """A saved answer: NAME, NAME.gz, or None. JSON is parsed, anything else returned as bytes.

    A saved file that is there but damaged (a truncated download, a broken archive, half a JSON
    document) is refused with NotFiled, never read as if it were empty and never a traceback.
    """
    for candidate in (Path(offline) / name, Path(offline) / f"{name}.gz"):
        if candidate.exists():
            try:
                raw = candidate.read_bytes()
                if candidate.suffix == ".gz":
                    raw = gzip.decompress(raw)
                body = json.loads(raw) if name.endswith(".json") else None
            except (OSError, EOFError, ValueError, zlib.error) as err:
                raise NotFiled(f"the saved file {candidate.name} is damaged and cannot be read "
                               f"({err})") from None
            if name.endswith(".json"):
                # A fixture written by tests/fixtures/capture.py wraps the answer with provenance.
                return body["data"] if isinstance(body, dict) and "_source" in body else body
            return raw
    return None


LEGACY_ENDS = "2024-12-31"      # the last quarter the older listing carries


def listing(symbol: str, offline: str | None = None,
            problems: list[str] | None = None) -> list[dict]:
    """Every quarterly results filing the exchange lists for a symbol, newest first.

    One row per quarter and basis (consolidated or standalone). A revised filing replaces the
    original, and the newer listing replaces the older one for the same quarter.

    When the older listing (quarters to Dec-2024) cannot be read, the newer one is still used, and
    the failure is appended to `problems`. It is never silent: without the older listing every
    year-ago figure and every year check that reaches back to 2024 is missing, and a missing check
    must not read as a passed one.
    """
    symbol = symbol.upper()
    if offline:
        integrated = _saved(offline, f"{symbol}-integrated.json")
        legacy = _saved(offline, f"{symbol}-legacy.json")
        if integrated is None and legacy is None:
            raise NotFiled(f"{symbol}: nothing saved for it in {offline}")
        if legacy is None and problems is not None:
            problems.append(f"the older results listing (quarters to {LEGACY_ENDS}) is not saved "
                            f"in {offline}")
    else:
        asked = quote(symbol, safe="")          # M&M is a real symbol; unescaped it asks for M
        integrated = fetch_json(INTEGRATED.format(symbol=asked), PAGES["integrated"])
        try:
            legacy = fetch_json(LEGACY.format(symbol=asked), PAGES["legacy"])
        except NotPublished as err:
            legacy = []
            if problems is not None:
                problems.append(f"the older results listing (quarters to {LEGACY_ENDS}) could "
                                f"not be read: {err}")

    rows: dict[tuple, dict] = {}
    if not isinstance(legacy, list):
        if legacy is not None and problems is not None:
            problems.append(f"the older results listing (quarters to {LEGACY_ENDS}) answered in "
                            f"a shape this does not read")
        legacy = []
    for row in legacy:
        if not isinstance(row, dict):
            continue
        end = _day(row.get("toDate"))
        if not end or not isinstance(row.get("xbrl"), str) or not row["xbrl"].strip():
            continue
        basis = "consolidated" if (row.get("consolidated") or "").lower() == "consolidated" \
            else "standalone"
        found = {"quarter_end": end.isoformat(), "basis": basis, "source": "legacy",
                 "company": (row.get("companyName") or "").strip() or None,
                 "filed": _stamp(row.get("broadCastDate")) or _stamp(row.get("filingDate")),
                 "xbrl": row["xbrl"], "page": None, "revision": row.get("reInd")}
        key = (found["quarter_end"], basis)
        if key not in rows or (found["filed"] or "") > (rows[key]["filed"] or ""):
            rows[key] = found
    newer = ((integrated.get("data") or []) if isinstance(integrated, dict) else integrated)
    for row in (newer if isinstance(newer, list) else []):
        if not isinstance(row, dict):
            continue
        end = _day(row.get("qe_Date"))
        if not end or not isinstance(row.get("xbrl"), str) or not row["xbrl"].strip():
            continue
        basis = (row.get("consolidated") or "").strip().lower()
        if basis not in ("consolidated", "standalone"):
            continue
        found = {"quarter_end": end.isoformat(), "basis": basis, "source": "integrated",
                 "company": (row.get("smName") or row.get("cmName") or "").strip() or None,
                 "filed": _stamp(row.get("broadcast_Date")), "xbrl": row["xbrl"],
                 "page": row.get("ixbrl"), "revision": row.get("type_Sub")}
        key = (found["quarter_end"], basis)
        old = rows.get(key)
        if old is None or old["source"] == "legacy" or (found["filed"] or "") > (old["filed"] or ""):
            rows[key] = found
    return sorted(rows.values(), key=lambda r: (r["quarter_end"], r["basis"]), reverse=True)


def parse(raw: bytes) -> dict:
    """One filing's quarter: its dates, basis, currency and the figures filed for it.

    Only the quarter's own context is read, by id, and its dates must span a quarter. Values are
    as filed, in rupees (or whatever currency the filing names, which the caller must check).
    """
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as err:
        raise NotFiled(f"the filing is not readable XBRL: {err}") from err

    context = next((c for c in root.iter(f"{XBRLI}context") if c.get("id") == QUARTER), None)
    if context is None:
        raise NotFiled("the filing has no quarter context (OneD)")
    if context.find(f"{XBRLI}entity/{XBRLI}segment") is not None:
        raise NotFiled("the filing's quarter context is a segment breakdown, not the total")
    start = _day(context.findtext(f"{XBRLI}period/{XBRLI}startDate"))
    end = _day(context.findtext(f"{XBRLI}period/{XBRLI}endDate"))
    if not start or not end or not 85 <= (end - start).days <= 95:
        raise NotFiled(f"the filing's quarter runs {start} to {end}, which is not a quarter")

    facts: dict[str, dict[str, str]] = {}
    for element in root:
        ref = element.get("contextRef")
        if ref in (QUARTER, YEAR_TO_DATE) and element.text is not None:
            name = element.tag.rsplit("}", 1)[-1]
            facts.setdefault(name, {})[ref] = element.text.strip()

    stated_end = _day(facts.get("DateOfEndOfReportingPeriod", {}).get(QUARTER))
    if stated_end and stated_end != end:
        raise NotFiled(f"the filing says its period ends {stated_end} but dates its quarter {end}")
    stated_start = _day(facts.get("DateOfStartOfReportingPeriod", {}).get(QUARTER))
    if stated_start and stated_start != start:
        raise NotFiled(f"the filing says its period starts {stated_start} but dates its quarter "
                       f"from {start}")

    currency = (facts.get("DescriptionOfPresentationCurrency", {}).get(QUARTER) or "").upper()
    if not currency:
        raise NotFiled("currency not stated")
    year_start, year_end, fiscal = financial_year(
        _day(facts.get("DateOfStartOfFinancialYear", {}).get(QUARTER)),
        _day(facts.get("DateOfEndOfFinancialYear", {}).get(QUARTER)), start, end)

    bank = "InterestEarned" in facts
    names = BANKING if bank else INDAS

    def number(element: str, ref: str) -> float | None:
        text = facts.get(element, {}).get(ref)
        if text in (None, ""):
            return None
        try:
            value = float(text)
        except ValueError:
            # Present but not a number ("1,234", "abc"): read as blank, a cost line would drop out
            # of the arithmetic as if it were zero and a margin would come out wrong in silence.
            raise NotFiled(f"the filing gives {element} as {text!r}, which is not a figure") \
                from None
        # float() reads "NaN" and "INF" happily, and a nan then compares false with everything:
        # it would print as a growth of "nan%" and pass every check unseen. Not a figure.
        if not math.isfinite(value):
            raise NotFiled(f"the filing gives {element} as {text!r}, which is not a figure")
        return value

    basis = (facts.get("NatureOfReportStandaloneConsolidated", {}).get(QUARTER) or "").lower()
    values = {key: number(element, QUARTER) for key, element in names.items()}
    to_date = {key: number(element, YEAR_TO_DATE) for key, element in names.items()}
    if (values.get("revenue") is None and values.get("profit") is None
            and values.get("profit_owners") is None):
        # A filing under a statement format this does not read (an insurer's, or a renamed
        # taxonomy) carries none of these names. A row of blanks would look like a quarter with
        # nothing in it, so the filing is refused and the reason listed instead.
        raise NotFiled("the filing carries no revenue and no profit under the names read here "
                       "(a statement format this does not read)")
    if basis != "consolidated":
        # A standalone statement has no outside shareholders to share the profit with, so the
        # owners' profit is the profit. Said here once rather than left as a blank.
        values["profit_owners"] = values["profit_owners"] or values["profit"]
        to_date["profit_owners"] = to_date["profit_owners"] or to_date["profit"]
    # The year to date's own dates. The older filings date the FourD CONTEXT as the quarter while
    # its figures are the year to date (Reliance, March 2024: context January to March, revenue
    # 9,14,472 crore, the whole year). The period the filing states for FourD is the true one, so
    # it is read first and the context is the fallback.
    ytd_context = next((c for c in root.iter(f"{XBRLI}context") if c.get("id") == YEAR_TO_DATE),
                       None)
    ytd_start = (_day(facts.get("DateOfStartOfReportingPeriod", {}).get(YEAR_TO_DATE))
                 or (_day(ytd_context.findtext(f"{XBRLI}period/{XBRLI}startDate"))
                     if ytd_context is not None else None))
    ytd_end = (_day(facts.get("DateOfEndOfReportingPeriod", {}).get(YEAR_TO_DATE))
               or (_day(ytd_context.findtext(f"{XBRLI}period/{XBRLI}endDate"))
                   if ytd_context is not None else None))
    return {
        "kind": "bank" if bank else "company",
        "start": start.isoformat(), "end": end.isoformat(),
        "basis": basis or None,
        "currency": currency,
        "year_start": year_start.isoformat(), "year_end": year_end.isoformat(),
        "fiscal": fiscal, "fy": fiscal.split()[1],
        "values": {k: v for k, v in values.items() if v is not None},
        "year_to_date": ({"start": ytd_start.isoformat(),
                          "end": (ytd_end or end).isoformat(),
                          "values": {k: v for k, v in to_date.items() if v is not None}}
                         if ytd_start else None),
    }


def financial_year(year_start: date | None, year_end: date | None, start: date,
                   end: date) -> tuple[date, date, str]:
    """The company's own financial year, as its filing states it, and the quarter's name in it.

    Most Indian companies run April to March, not all: a company with an October year names its
    September quarter Q4, not Q2. So the name comes from the year the filing states, and a quarter
    that does not fall on a quarter of that year is refused rather than named by a guess.
    """
    if year_start is None or year_end is None:
        raise NotFiled("the filing does not state its financial year")
    after = end + timedelta(days=1)
    months = (after.year - year_start.year) * 12 + after.month - year_start.month
    first = start.year * 12 + start.month - (year_start.year * 12 + year_start.month)
    if (year_start.day != 1 or after.day != 1 or start.day != 1 or months not in (3, 6, 9, 12)
            or first != months - 3 or end > year_end):
        raise NotFiled(f"the quarter {start} to {end} is not a quarter of the company's financial "
                       f"year {year_start} to {year_end}")
    return year_start, year_end, f"Q{months // 3} FY{year_end.year % 100:02d}"


def read(filing: dict, offline: str | None = None) -> dict:
    """The parsed quarter for one listing row, fetched or read from a saved copy."""
    name = filing["xbrl"].rsplit("/", 1)[-1]
    if offline:
        raw = _saved(offline, name)
        if raw is None:
            raise NotFiled(f"{name} is not saved in {offline}")
    else:
        raw = fetch(filing["xbrl"], PAGES["integrated"])
    found = parse(raw)
    if found["end"] != filing["quarter_end"]:
        raise NotFiled(f"the exchange lists this filing for {filing['quarter_end']} but the file "
                       f"itself is for {found['end']}")
    # One basis for a whole series is the rule, and the listing is what picks the basis. A file
    # that says it is the other one (or does not say) would mix the two in silence.
    if found["basis"] != filing.get("basis"):
        raise NotFiled(f"the exchange lists this filing as {filing.get('basis')} but the file "
                       f"itself says {found['basis'] or 'neither consolidated nor standalone'}")
    return dict(found, filing=filing)


def board_meetings(symbol: str, offline: str | None = None) -> list[dict]:
    """Board meetings the company has told the exchange about, newest first."""
    symbol = symbol.upper()
    if offline:
        rows = _saved(offline, f"{symbol}-board-meetings.json")
        if rows is None:
            return []
    else:
        rows = fetch_json(BOARD_MEETINGS.format(symbol=quote(symbol, safe="")), PAGES["meetings"])
    out = []
    for row in rows if isinstance(rows, list) else []:
        when = _day(row.get("bm_date")) if isinstance(row, dict) else None
        if not when:
            continue
        out.append({"date": when.isoformat(),
                    "purpose": str(row.get("bm_purpose") or "").strip(),
                    "about": str(row.get("bm_desc") or "").strip(),
                    "told": _stamp(row.get("bm_timestamp")),
                    "link": row.get("attachment")})
    return sorted(out, key=lambda r: r["date"], reverse=True)


# The documents a company files on its results day, by the exchange's own categories, read off its
# announcements. The results themselves arrive as "Outcome of Board Meeting" with text saying the
# financial results were submitted; the press release and the call presentation follow the same day.
RESULT_DOCUMENTS = (
    ("results", "Outcome of Board Meeting", r"financial results"),
    ("press release", "Press Release", r""),
    ("presentation", "Analysts/Institutional Investor Meet/Con. Call Updates", r"presentation"),
)


def results_documents(symbol: str, filed: str, offline: str | None = None) -> list[dict]:
    """The results document, press release and presentation filed around `filed`, if any.

    The US pack links the earnings 8-K's press release beside the 10-Q. The Indian counterpart is
    on the exchange's announcements, the day the board approved the results.
    """
    import re
    from datetime import timedelta

    from .nse import announcements

    day = _day(str(filed or "")[:10])
    if day is None:
        return []
    # An announcements list that cannot be read raises (NotPublished live, NotFiled for a damaged
    # saved copy), so the caller can say so: an empty answer would read as "no press release".
    if offline:
        rows = _saved(offline, f"{symbol.upper()}-announcements.json") or []
    else:
        rows = announcements(symbol, 2, day + timedelta(days=1))
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    window = {(day + timedelta(days=k)).isoformat() for k in (-1, 0, 1)}
    out = []
    for kind, category, words in RESULT_DOCUMENTS:
        for row in rows:
            when = _stamp(row.get("an_dt"))
            text = str(row.get("attchmntText") or "").strip()
            if (when and when[:10] in window and str(row.get("desc") or "").strip() == category
                    and re.search(words, text, re.I) and row.get("attchmntFile")):
                out.append({"kind": kind, "when": when, "link": row["attchmntFile"],
                            "said": text[:200]})
                break
    return out


def results_meeting(meetings: list[dict], today: date,
                    newest_filed: str | None = None) -> dict | None:
    """The next board meeting called to approve results, on or after today, or None.

    A meeting on or before the newest results filing has already produced it, so it is not the
    next one, even on its own day: TCS met on 8 October 2026 and filed that evening, and calling
    8 October "next" that night would send a trader looking for results already out. A meeting
    today with no filing after it yet is still ahead, and is the one that matters most.

    None means none has been announced, which is not the same as none coming: companies give
    notice only days ahead. The caller says which.
    """
    done = (newest_filed or "")[:10]
    ahead = [m for m in meetings if m["date"] >= today.isoformat() and m["date"] > done
             and "result" in (m["purpose"] + " " + m["about"]).lower()]
    return min(ahead, key=lambda m: m["date"]) if ahead else None
