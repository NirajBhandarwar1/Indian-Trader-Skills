#!/usr/bin/env python3
"""The Results Analyst. What a company filed with the exchange, quarter by quarter.

Reads each quarter's results as the company filed them with NSE (XBRL), lines them up, and says
plainly what changed: revenue, margins, owners' profit and diluted EPS, each against the same
quarter a year earlier, with the last four quarters added up. It also says when the next results
board meeting is, from the exchange's own notice, so a trade can be checked against it.

  results.py TCS                        the last six quarters, with links to the filings
  results.py TCS SBIN --quarters 8      two years
  results.py TCS --metrics              merge a row per company into nse/metrics.json
  results.py TCS --annual               four years of accounts and their ratios (--ratios, --shares)

Nothing here is estimated or filled in. An EPS growth figure that would compare across a bonus or a
split is withheld, and growth leaning on a year whose quarters do not add up to the year as filed
is marked "restated base", each with the reason printed.

Standard library only.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
from datetime import date, datetime
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import actions, clock, filings, nse, settings, shares, show, yahoo   # noqa: E402

CRORE = 1e7
DEFAULTS = {"results_quarters": 6, "results_basis": "consolidated"}
SOURCE = "NSE results filings (XBRL), as filed by the company"
NAMES = {"revenue": "revenue", "profit_owners": "owners' profit"}
TTM_NAMES = {"revenue": "Revenue", "profit_owners": "Owners' profit", "eps_diluted": "EPS"}
LEGACY = "*older listing*"      # the key in `unread` when the older listing could not be read


# ---------------------------------------------------------------- the quarters

def fiscal(end: str) -> str:
    """'2026-09-30' -> 'Q2 FY27' for a company whose year runs April to March.

    The report itself names each quarter by the financial year its own filing states (see
    filings.financial_year), because not every company runs April to March. This is the April
    reading only, for a date with no filing behind it.
    """
    when = date.fromisoformat(end)
    quarter = {6: 1, 9: 2, 12: 3, 3: 4}.get(when.month)
    if not quarter:
        raise ValueError(f"{end} is not the end of an April-to-March quarter")
    year = when.year + (1 if when.month > 3 else 0)
    return f"Q{quarter} FY{year % 100:02d}"


def year_ago(end: str) -> str:
    """The same month end a year earlier: 29 February becomes 28 February, not an error."""
    when = date.fromisoformat(end)
    return when.replace(year=when.year - 1,
                        day=calendar.monthrange(when.year - 1, when.month)[1]).isoformat()


def day_before(day: str) -> str:
    """'2026-07-01' -> '2026-06-30': where the quarter before one starting on `day` ends."""
    return date.fromordinal(date.fromisoformat(day).toordinal() - 1).isoformat()


def pct(now, then):
    if now is None or then in (None, 0):
        return None
    return round(100.0 * (now - then) / abs(then), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def gross_profit(q: dict) -> float | None:
    """Revenue less the cost of goods it FILED: materials, purchases and the inventory change.

    None for a bank, and None for a company that files no cost of goods (TCS files zero for all
    three; its equipment and licence costs sit inside other expenses). A 100% gross margin for a
    software company would be arithmetically true and useless, so it is left blank and said so.
    """
    v = q["values"]
    if q["kind"] == "bank" or v.get("revenue") is None:
        return None
    parts = [v.get(k) for k in ("materials", "purchases", "inventory_change")]
    # One of the three not filed is not a zero: taking it as one would print a margin built on a
    # guess. Every real filing read carries all three, so this only ever blanks a damaged one.
    if any(p is None for p in parts) or not any(parts):
        return None
    return v["revenue"] - sum(parts)


def operating_profit(q: dict) -> tuple[float | None, bool]:
    """(operating profit, filed?).

    A bank files one: operating profit before provisions. An Ind AS company does not, so it is
    computed from filed lines: profit before exceptional items and tax, plus finance costs, less
    other income. That is the profit of the business itself, and it is labelled as computed.
    """
    v = q["values"]
    if q["kind"] == "bank":
        return v.get("operating_profit"), True
    # Finance costs or other income not filed is not a zero: computed without one, the operating
    # profit would be off by exactly that line, with nothing on screen to say so.
    if any(v.get(k) is None for k in ("profit_before_exceptional", "finance_costs",
                                      "other_income")):
        return None, False
    return v["profit_before_exceptional"] + v["finance_costs"] - v["other_income"], False


def rounding(q: dict) -> float:
    """The most a filed figure can be off from its true value by rounding: half a unit of what it
    is rounded to. Measured, the big companies file to the crore and the small to the lakh, so the
    unit is read from the figures themselves rather than assumed."""
    values = [abs(x) for k, x in q["values"].items() if x and not k.startswith("eps")]
    for unit in (CRORE, 1e5, 1e3, 1.0):
        if values and all(abs(x / unit - round(x / unit)) < 1e-6 for x in values):
            return unit / 2
    return 0.5


def restated_years(by_end: dict[str, dict]) -> dict[str, str]:
    """Financial years whose four quarters, as filed, do not add up to the year as filed.

    The March filing carries the full year beside its quarter. Measured over 7 companies and two
    years, the four quarters add up to the year exactly or within their rounding in all but a few
    cases, and those are far apart: ITC's FY2025 revenue by 1,425 crore (the year its hotels were
    demerged), Suzlon's standalone FY2025 by 1,437 crore, HDFC Bank's consolidated FY2025 owners'
    profit by 2,198 crore. Why each disagrees is not in the filings, so no reason is given; the
    fact that they disagree is enough to say a growth figure leaning on that year may compare two
    different things, and it is flagged rather than shown as if it were clean.
    """
    return {fy: text for fy, (state, text) in year_checks(by_end).items() if state == "restated"}


def _quarter_ends(year_start: str) -> list[str]:
    """The four quarter ends of a financial year starting on `year_start` (a 1st of a month)."""
    first = date.fromisoformat(year_start)
    out = []
    for k in (3, 6, 9, 12):
        month = first.month - 1 + k
        year, month = first.year + month // 12, month % 12 + 1
        after = date(year, month, 1)
        out.append(date.fromordinal(after.toordinal() - 1).isoformat())
    return out


def year_checks(by_end: dict[str, dict], unread: dict[str, str] | None = None,
                newest: str | None = None) -> dict[str, tuple[str, str]]:
    """Each financial year the loaded quarters touch: {'FY25': (state, text)}.

    state is "clean", "restated" (the four quarters as filed do not add up to the year as
    filed), or "unchecked" (the check could not be made, and `text` says why). A year still in
    progress has no year figure to disagree with and is left out.

    Only a year whose year-end filing carries the WHOLE year is tested: the year to date must
    start on the day the filing says the financial year starts and run about 365 days. A company
    whose year ends in September files six months to date in March, and comparing four quarters
    with that would print a false disagreement.

    `unread` maps a quarter end to why its filing could not be read; `newest` is the newest
    quarter loaded, so a year ending after it is still in progress.
    """
    unread = unread or {}
    years: dict[str, dict] = {}
    for q in by_end.values():
        years.setdefault(q["fy"], q)
    out = {}
    for fy, any_q in years.items():
        year_end = any_q["year_end"]
        if newest and year_end > newest:
            continue
        ends = _quarter_ends(any_q["year_start"])
        missing = [e for e in ends if e not in by_end]
        if missing:
            def reason(e: str) -> str:
                if e in unread:
                    return unread[e]
                if LEGACY in unread and e <= filings.LEGACY_ENDS:
                    return unread[LEGACY]
                return "not listed on this basis by the exchange"
            why = "; ".join(f"{e}: {reason(e)}" for e in missing)
            out[fy] = ("unchecked", f"the filings for {fy} are not all here ({why})")
            continue
        last = by_end[ends[-1]]
        ytd = last.get("year_to_date")
        if (not ytd or ytd["start"] != any_q["year_start"]
                or not 350 <= (date.fromisoformat(ytd.get("end") or ends[-1])
                               - date.fromisoformat(ytd["start"])).days <= 380):
            out[fy] = ("unchecked", f"the filing for {ends[-1]} does not carry the whole of {fy} "
                                    f"beside its quarter")
            continue
        out[fy] = ("clean", "")
        for key in ("revenue", "profit_owners"):
            whole = ytd["values"].get(key)
            parts = [by_end[e]["values"].get(key) for e in ends]
            if whole is None or any(p is None for p in parts):
                continue
            allowed = sum(rounding(by_end[e]) for e in ends) + rounding(last)
            if abs(sum(parts) - whole) > allowed:
                out[fy] = ("restated", f"its four quarters as filed add to {money(sum(parts))} of "
                                       f"{NAMES[key]}, but the year was filed as {money(whole)}")
                break
    return out


def basis_moved(corporate: list[dict] | None, since: str, until: str) -> str | None:
    """Why per-share figures from `since` and `until` cannot be compared, or None if they can.

    Fail-safe: no corporate-action record, or an action this cannot read exactly, refuses too.
    """
    if corporate is None or not isinstance(corporate, list):
        return "the exchange's record of bonuses and splits could not be read, so it is unchecked"
    # actions.basis_change fails closed: an entry that is not a record, or a bonus or split whose
    # ex-date cannot be read (it could fall inside the window), raises Uncertain and refuses.
    try:
        factor, why = actions.basis_change(corporate, date.fromisoformat(since),
                                           date.fromisoformat(until))
    except actions.Uncertain as err:
        return f"a corporate action between the two cannot be read exactly: {err}"
    if factor != 1.0:
        return f"the share basis changed between them: {'; '.join(why)}"
    return None


def base_notes(now: dict, then: dict, end: str) -> list[str]:
    """Facts about the year-ago quarter that a growth figure leans on. Figures only, no reasons.

    Measured on Reliance: its June 2025 quarter carried 15,119 crore of other income against 4,482
    to 6,550 crore in the four quarters after it, so its June 2026 profit reads -22.4% against a
    base that was unusually large outside the business. The filings show how large, not why, so
    the note gives the two figures and nothing else. Said when the base's other income was more
    than twice this quarter's and more than a tenth of its profit before tax, or when the base
    carried exceptional items.
    """
    out = []
    if not then:
        return out
    oi_now, oi_then, pbt_then = now.get("other_income"), then.get("other_income"), then.get("pbt")
    if (oi_then and pbt_then and oi_now is not None and oi_then > 2 * max(oi_now, 0)
            and oi_then > 0.1 * abs(pbt_then)):
        out.append(f"other income a year earlier ({year_ago(end)}) was {money(oi_then)}, against "
                   f"{money(oi_now)} this quarter: the growth figures compare with that base")
    if then.get("exceptional"):
        out.append(f"the quarter a year earlier ({year_ago(end)}) had exceptional items of "
                   f"{money(then['exceptional'])} in its profit before tax")
    # Discontinued operations sit inside the profit but are not the business that is left. ITC's
    # March 2025 quarter carried 14,652.61 crore of it (the hotels), so its March 2026 profit
    # reads -72.7% against a base three quarters of which was a business no longer there.
    if then.get("discontinued") or now.get("discontinued"):
        def filed(value):         # a figure the filing does not carry is not shown as a zero
            return "none filed" if value is None else money(value)
        out.append(f"profit from discontinued operations was {filed(then.get('discontinued'))}"
                   f" a year earlier ({year_ago(end)}) and {filed(now.get('discontinued'))} "
                   f"this quarter, inside the profit both growth figures compare")
    return out


def filed_day(q: dict) -> str | None:
    """The day the quarter's figures were published, which is when its per-share basis is fixed.

    A bonus that goes ex after the quarter ends but before its results are approved restates
    that quarter's EPS (Ind AS 33.64), so a window that closed at the quarter end would compare
    a restated figure with an unrestated one. The publication day is on or after the approval,
    so closing the window there can only withhold more, never less.
    """
    return ((q.get("filing") or {}).get("filed") or "")[:10] or None


def analyse(quarters: list[dict], count: int = 6, corporate: list[dict] | None = None,
            unread: dict[str, str] | None = None) -> dict:
    """Every number in the report. `quarters` are parsed filings, any order, one basis.

    `unread` maps a quarter end to why its filing could not be read, so a year check that needed
    it says so rather than passing in silence.
    """
    by_end = {q["end"]: q for q in quarters}
    ends = sorted(by_end, reverse=True)
    checks = year_checks(by_end, unread, ends[0] if ends else None)
    restated = {fy: text for fy, (state, text) in checks.items() if state == "restated"}
    rows = []
    for end in ends[:count]:
        q = by_end[end]
        v = q["values"]
        before = by_end.get(year_ago(end))
        b = before["values"] if before else {}
        gp = gross_profit(q)
        op, op_filed = operating_profit(q)
        eps, eps_before = v.get("eps_diluted"), b.get("eps_diluted")
        notes = []
        eps_yoy = None
        if eps is not None and eps_before is not None:
            closes = filed_day(q)
            moved = (basis_moved(corporate, year_ago(end), closes) if closes else
                     "the day this quarter's results were published is not known")
            if moved:
                notes.append(f"EPS growth withheld: {moved}")
            else:
                eps_yoy = pct(eps, eps_before)
        leans = sorted({x["fy"] for x in (q, before)} if before else set())
        leaning = [fy for fy in leans if fy in restated]
        unchecked = [fy for fy in leans if checks.get(fy, ("",))[0] == "unchecked"]
        for fy in leaning:
            notes.append(f"growth compares with {fy}, and {restated[fy]}")
        for fy in unchecked:
            notes.append(f"restated check not possible: {checks[fy][1]}")
        notes += base_notes(v, b, end)
        rows.append({
            "end": end, "start": q["start"], "fiscal": q["fiscal"], "kind": q["kind"],
            "basis": q["basis"],
            "discontinued": v.get("discontinued"),
            "discontinued_year_ago": b.get("discontinued"), "restated_unchecked": unchecked,
            "revenue": v.get("revenue"), "gross_profit": gp, "operating_profit": op,
            "operating_profit_filed": op_filed, "profit_owners": v.get("profit_owners"),
            "profit": v.get("profit"), "pbt": v.get("pbt"), "exceptional": v.get("exceptional"),
            "other_income": v.get("other_income"), "eps_diluted": eps,
            "gross_margin": margin(gp, v.get("revenue")),
            "operating_margin": margin(op, v.get("revenue")),
            "net_margin": margin(v.get("profit_owners"), v.get("revenue")),
            "revenue_yoy": pct(v.get("revenue"), b.get("revenue")),
            "profit_yoy": pct(v.get("profit_owners"), b.get("profit_owners")),
            "eps_yoy": eps_yoy, "restated_base": bool(leaning), "notes": notes,
            "filed": q["filing"]["filed"], "link": q["filing"]["page"] or q["filing"]["xbrl"],
            "xbrl": q["filing"]["xbrl"],
        })

    ttm, ttm_notes = {}, []
    last4 = ends[:4]
    if len(last4) == 4 and (date.fromisoformat(last4[0]) - date.fromisoformat(last4[-1])).days < 300:
        for key in ("revenue", "profit_owners", "eps_diluted"):
            parts = [by_end[e]["values"].get(key) for e in last4]
            if all(p is not None for p in parts):
                ttm[key] = sum(parts)
            else:
                # Three quarters are not four: the total is left out, and why is said.
                gaps = ", ".join(e for e, p in zip(last4, parts) if p is None)
                ttm_notes.append(f"{TTM_NAMES[key]} for the last four quarters not given: the "
                                 f"filing for {gaps} carries none")
        if "eps_diluted" in ttm:
            first_start = by_end[last4[-1]]["start"]
            closes = filed_day(by_end[last4[0]])
            moved = (basis_moved(corporate, first_start, closes) if closes else
                     "the day the newest quarter's results were published is not known")
            if moved:
                del ttm["eps_diluted"]
                ttm_notes.append(f"EPS for the last four quarters withheld: {moved}")
            else:
                ttm["eps_diluted"] = round(ttm["eps_diluted"], 2)
        ttm["from"], ttm["to"] = by_end[last4[-1]]["start"], last4[0]
    return {"rows": rows, "ttm": ttm, "ttm_notes": ttm_notes, "restated": restated}


def changes(result: dict) -> list[str]:
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    cur, prev = rows[0], (rows[1] if len(rows) > 1 else None)
    # "The quarter before" must be the quarter before. When its filing could not be read, the
    # next row is two quarters back, and comparing with it under that name would be false.
    if prev and (not cur.get("start") or prev["end"] != day_before(cur["start"])):
        prev = None

    def said(value):
        return "n/a" if value is None else money(value)

    top = "total income" if cur["kind"] == "bank" else "revenue"
    line = f"Quarter ending {cur['end']} ({cur['fiscal']}): {top} {said(cur['revenue'])}"
    if cur["revenue_yoy"] is not None:
        line += f" ({cur['revenue_yoy']:+.1f}% vs a year earlier)"
    line += f", owners' profit {said(cur['profit_owners'])}"
    if cur["profit_yoy"] is not None:
        line += f" ({cur['profit_yoy']:+.1f}%)"
    if cur["eps_diluted"] is not None:
        line += f", diluted EPS Rs {cur['eps_diluted']:.2f}"
        if cur["eps_yoy"] is not None:
            line += f" ({cur['eps_yoy']:+.1f}%)"
    out = [line + "."]
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        text = f"{name} {cur[key]:.1f}%"
        if prev and prev[key] is not None:
            text += f" ({cur[key] - prev[key]:+.1f} points vs the quarter before)"
        out.append(text + ".")
    if cur["exceptional"]:
        out.append(f"Exceptional items of {money(cur['exceptional'])} are inside profit before tax "
                   f"this quarter. Read the filing before reading the profit as growth.")
    if cur.get("discontinued"):
        out.append(f"Profit from discontinued operations of {money(cur['discontinued'])} is inside "
                   f"the profit this quarter: it is not the business that is left.")
    if cur.get("discontinued_year_ago"):
        out.append(f"The quarter a year earlier ({year_ago(cur['end'])}) carried "
                   f"{money(cur['discontinued_year_ago'])} of profit from discontinued operations: the "
                   f"profit and EPS growth compare with that base.")
    if (cur["profit_owners"] is not None and cur["operating_profit"] is not None
            and cur["profit_owners"] > cur["operating_profit"]):
        # Arithmetic, not a reason: profit can only exceed the operating line through what sits
        # between them, and the sentence names those lines and nothing else.
        if cur["kind"] == "bank":
            out.append("Owners' profit is above operating profit before provisions: something "
                       "outside that line (provisions written back, exceptional items, associates "
                       "or a tax credit) is inside it. Check the filing before reading it as "
                       "growth.")
        else:
            out.append("Owners' profit is above operating profit: something outside the business's "
                       "own line (other income, exceptional gains, associates, discontinued "
                       "operations or a tax credit) is inside it. Check the filing before reading "
                       "it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append(f"{top.capitalize()} {pct(cur['revenue'], prev['revenue']):+.1f}% vs the "
                   f"quarter before (many businesses are seasonal: compare with a year earlier "
                   f"first).")
    return out


def money(value) -> str:
    if value is None:
        return "-"
    crore = value / CRORE
    # A figure under half a lakh is not zero: to two places it would print as "Rs 0 cr" (or
    # "Rs -0 cr"), so it is given to the rupee instead.
    places = 2 if value == 0 or abs(crore) >= 0.005 else 7
    text = f"{crore:,.{places}f}".rstrip("0").rstrip(".")
    return f"Rs {text} cr"


def _cr(value) -> str:
    return "-" if value is None else f"{value / CRORE:,.0f}"


def _p(value) -> str:
    """A growth figure with its sign, and an exact zero as plain 0.0% (as the US pack prints it)."""
    if value is None:
        return "-"
    return f"{value:+.1f}%" if value != 0 else "0.0%"


def _pm(value) -> str:
    return "-" if value is None else f"{value:.1f}%"


# ---------------------------------------------------------------- the filings around it

def stale_note(result: dict, meetings: list[dict], today: date,
               listed: list[dict] | None = None, basis: str | None = None) -> str | None:
    """A note when the board has approved results the exchange has no XBRL for yet.

    `listed` is every row the exchange's listing carries. When a filing published on or after the
    meeting is there and was not read, "not on the exchange yet" would be false, so the note says
    which it is instead.
    """
    if not result["rows"]:
        return None
    newest_filed = (result["rows"][0]["filed"] or "")[:10]
    if not newest_filed:
        return None               # no publication day to compare a meeting with
    # Strictly before today: a meeting today may not have happened yet (the notice gives the day,
    # not the hour), and it is reported as the next meeting instead. Saying "the board met" about
    # it would be false until it has.
    held = [m for m in meetings if newest_filed < m["date"] < today.isoformat()
            and "result" in (m["purpose"] + " " + m["about"]).lower()]
    if not held:
        return None
    meeting = max(held, key=lambda m: m["date"])
    # Only a quarter newer than the newest read: a revision of an older one, filed later, is not
    # the results this meeting approved.
    since = [r for r in listed or [] if (r.get("filed") or "")[:10] >= meeting["date"]
             and r.get("quarter_end", "") > result["rows"][0]["end"]]
    if any(r.get("basis") == basis for r in since):
        return (f"the board met on {meeting['date']} to approve results, and the exchange lists "
                f"a {basis} filing published since, but it could not be read here (see 'Not "
                f"read'). Read the announcement directly: {meeting['link']}")
    if since:
        return (f"the board met on {meeting['date']} to approve results; the exchange has the "
                f"{since[0].get('basis')} filing from it but not yet the {basis} one, so the "
                f"newest {basis} filing here is {newest_filed}. Read the announcement directly: "
                f"{meeting['link']}")
    return (f"the board met on {meeting['date']} to approve results, after the newest filing "
            f"here ({newest_filed}), and their XBRL is not on the exchange yet. Read the "
            f"announcement directly: {meeting['link']}")


def next_results(meetings: list[dict], today: date, newest_filed: str | None = None) -> str:
    """The next results meeting. One on or before the newest filing has already reported."""
    found = filings.results_meeting(meetings, today, newest_filed)
    if found:
        told = (f" (the company told the exchange on {found['told'][:10]})"
                if found.get("told") else "")
        return f"Next results board meeting: {found['date']}{told}. {found['about']}".rstrip()
    return ("No results board meeting has been announced yet. Companies give notice only days "
            "ahead, so this is not the same as none coming.")


# ---------------------------------------------------------------- one company

def quarters_for(symbol: str, count: int, basis: str, offline: str | None,
                 listed: list[dict] | None = None):
    """The parsed filings needed: `count` quarters plus the year before each, on one basis.

    One basis for the whole series. Consolidated against standalone is two different companies,
    so a quarter missing on the chosen basis is a gap, never filled from the other.

    How many: the `count` quarters shown, the four before them (each one's year-ago quarter),
    and three more, so the oldest year-ago quarter's whole financial year is here for the check
    that its quarters add up. Fewer than that and the check would pass for want of data.

    A filing that cannot be read pushes the shown rows one quarter older, so the year a shown row
    leans on can reach past that count. Every listed quarter such a year needs is then read too:
    left unread, its check would say the exchange does not list it, which would be false.

    Returns (quarters, basis, problems, unread): `unread` maps a quarter end to why it is
    missing, for the year check to quote. `listed`, when given, receives every listing row (both
    bases), so a caller can tell "not filed yet" from "filed and not readable".
    """
    problems: list[str] = []
    rows = filings.listing(symbol, offline, problems)
    if listed is not None:
        listed.extend(rows)
    if not rows:
        raise filings.NotFiled(f"{symbol}: the exchange lists no quarterly results")
    chosen = basis
    if not any(r["basis"] == chosen for r in rows[:2]):
        other = "standalone" if chosen == "consolidated" else "consolidated"
        if any(r["basis"] == other for r in rows[:2]):
            chosen = other
    on_basis = [r for r in rows if r["basis"] == chosen]
    found, unread = [], {}

    def take(row):
        try:
            found.append(filings.read(row, offline))
        except (filings.NotFiled, nse.NotPublished) as err:
            problems.append(f"{row['quarter_end']}: {err}")
            unread[row["quarter_end"]] = f"its filing could not be read ({err})"

    tried = set()
    todo = on_basis[:count + 7]
    while todo:
        for row in todo:
            tried.add(row["quarter_end"])
            take(row)
        by_end = {q["end"]: q for q in found}
        shown = sorted(by_end, reverse=True)[:count]
        leaned = [by_end[e] for e in shown] + [by_end[year_ago(e)] for e in shown
                                              if year_ago(e) in by_end]
        needed = {e for q in leaned for e in _quarter_ends(q["year_start"])}
        todo = [r for r in on_basis if r["quarter_end"] in needed - tried]
    legacy_gone = next((p for p in problems if p.startswith("the older results listing")), None)
    if legacy_gone:
        unread[LEGACY] = legacy_gone
    return found, chosen, problems, unread


def corporate_record(symbol: str, offline: str | None) -> list[dict] | None:
    if offline:
        for name in (f"{symbol}-actions.json", f"actions-{symbol}.json"):
            path = Path(offline) / name
            if path.exists():
                try:
                    body = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None           # damaged: unchecked, so EPS growth is withheld
                body = body["data"] if isinstance(body, dict) and "_source" in body else body
                return body if isinstance(body, list) else None
        return None
    try:
        return nse.corporate_actions(symbol)
    except Exception:                                            # noqa: BLE001
        return None


def one(symbol: str, count: int, basis: str, offline: str | None, no_filings: bool,
        today: date) -> dict:
    listed: list[dict] = []
    found, chosen, problems, unread = quarters_for(symbol, count, basis, offline, listed)
    if not found:
        raise filings.NotFiled(f"{symbol}: no quarterly filing could be read"
                               + (f" ({'; '.join(problems)})" if problems else ""))
    odd = sorted({str(q["currency"] or "a currency not stated") for q in found
                  if q["currency"] != "INR"})
    if odd:
        raise filings.NotFiled(f"{symbol}: filed in {', '.join(odd)}, not rupees, and not "
                               f"converted")
    result = analyse(found, count, corporate_record(symbol, offline), unread)
    newest = result["rows"][0] if result["rows"] else {}
    meetings, documents, stale, ahead = [], [], None, None
    if not no_filings:
        # The quarters are read; the notices around them are a second source. One that cannot be
        # read is said as that, never as "none announced", and does not throw the results away.
        try:
            meetings = filings.board_meetings(symbol, offline)
        except (filings.NotFiled, nse.NotPublished) as err:
            problems.append(f"board-meeting notices: {err}")
            ahead = ("The board-meeting notices could not be read, so whether a results meeting "
                     "has been announced is not known here.")
        else:
            stale = stale_note(result, meetings, today, listed, chosen)
            # A meeting that a published filing (either basis, read or not) came out of has
            # reported, so it is not "next" even when that filing could not be read here.
            # Only the newest quarter read and newer ones count: a revision of an older one filed
            # today is not the results a meeting today will approve.
            done = max([(newest.get("filed") or "")[:10]]
                       + [(r.get("filed") or "")[:10] for r in listed
                          if r["quarter_end"] > newest.get("end", "")]) or None
            ahead = next_results(meetings, today, done)
        if newest.get("filed"):
            try:
                documents = filings.results_documents(symbol, newest["filed"], offline)
            except (filings.NotFiled, nse.NotPublished) as err:
                problems.append(f"results-day announcements: {err}")
    return {
        "symbol": symbol, "company": found[0]["filing"].get("company"),
        "basis": chosen, "kind": found[0]["kind"], "source": SOURCE, "documents": documents,
        "result": result, "changes": changes(result), "problems": problems,
        "stale": stale, "next_results": ahead, "fell_back": chosen != basis,
    }


def render(found: dict, asked_basis: str) -> list[str]:
    r = found["result"]
    bank = found["kind"] == "bank"
    name = f" ({found['company']})" if found.get("company") else ""
    out = [f"{found['symbol']}{name}, {found['basis']} results. Rs crore except EPS. "
           f"Source: {found.get('source') or SOURCE}."]
    if found["fell_back"]:
        out.append(f"(No {asked_basis} filing for the latest quarters, so {found['basis']} for "
                   f"every quarter: the two are never mixed.)")
    if bank:
        out.append("A bank: 'revenue' is total income as filed (interest earned plus other "
                   "income), operating profit is before provisions as filed, and there is no "
                   "gross margin.")
    out.append("")
    out.append(f"{'quarter end':<11} {'fiscal':<8} {'revenue':>10} {'rev yoy':>8} {'gross':>6} "
               f"{'oper':>6} {'net':>6} {'profit':>9} {'prof yoy':>8} {'EPS dil':>8} "
               f"{'EPS yoy':>8}")
    for row in r["rows"]:
        out.append(f"{row['end']:<11} {row['fiscal']:<8} {_cr(row['revenue']):>10} "
                   f"{_p(row['revenue_yoy']):>8} {_pm(row['gross_margin']):>6} "
                   f"{_pm(row['operating_margin']):>6} {_pm(row['net_margin']):>6} "
                   f"{_cr(row['profit_owners']):>9} {_p(row['profit_yoy']):>8} "
                   f"{'-' if row['eps_diluted'] is None else format(row['eps_diluted'], '.2f'):>8} "
                   f"{_p(row['eps_yoy']):>8}" + ("  restated base" if row["restated_base"] else "")
                   + ("  base unchecked" if row["restated_unchecked"] else ""))
    flagged = [(row["fiscal"], note) for row in r["rows"] for note in row["notes"]]
    if flagged:
        out.append("")
        out.append("Withheld or flagged:")
        for when, note in flagged:
            out.append(f"  {when}: {note}.")
    t = r["ttm"]
    if t:
        out.append("")
        withheld = any(n.startswith("EPS for the last four quarters withheld")
                       for n in r["ttm_notes"])
        out.append(f"Last 4 quarters ({t['from']} to {t['to']}): revenue {money(t.get('revenue'))}, "
                   f"owners' profit {money(t.get('profit_owners'))}, diluted EPS "
                   + (f"Rs {t['eps_diluted']:.2f}." if t.get("eps_diluted") is not None
                      else "withheld." if withheld else "-."))
    for note in r["ttm_notes"]:
        out.append(f"  {note}.")
    out.append("")
    out.append("What changed:")
    for line in found["changes"]:
        out.append(f"  - {line}")
    out.append("")
    if not bank:
        out.append("'oper' is computed from filed lines: profit before exceptional items and tax, "
                   "plus finance costs, less other income. 'gross' is revenue less the materials, "
                   "purchases and inventory change filed; blank where all three are zero or one is "
                   "not filed, and either is blank where a line it needs is not filed. 'profit' is "
                   "the owners' share.")
    else:
        out.append("'profit' is the owners' share, after minority interest and associates.")
    newest = r["rows"][0] if r["rows"] else None
    if newest:
        out.append(f"Latest filing: {newest['fiscal']}, filed {(newest['filed'] or '')[:10]}  "
                   f"{newest['link']}")
    for doc in found.get("documents") or []:
        out.append(f"Results-day {doc['kind']}: filed {doc['when'][:16].replace('T', ' ')}  "
                   f"{doc['link']}")
    if found["next_results"]:
        out.append(found["next_results"])
    if found["stale"]:
        out.append("")
        out.append(f"NOTE: {found['stale']}")
    for problem in found["problems"]:
        out.append(f"Not read: {problem}")
    return out


def metrics_row(found: dict) -> dict:
    r = found["result"]
    cur = r["rows"][0] if r["rows"] else {}
    t = r["ttm"]
    return {"symbol": found["symbol"], "name": found.get("company"), "basis": found["basis"],
            "period_end": cur.get("end"),
            "fiscal": cur.get("fiscal"), "revenue_ttm": t.get("revenue"),
            "profit_owners_ttm": t.get("profit_owners"), "eps_ttm": t.get("eps_diluted"),
            "revenue_growth_yoy": cur.get("revenue_yoy"),
            "profit_growth_yoy": cur.get("profit_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "gross_margin": cur.get("gross_margin"),
            "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"), "notes": cur.get("notes", []),
            "ttm_notes": list(r.get("ttm_notes") or []), "source": SOURCE}


class Unmergeable(Exception):
    """The metrics file is there and is not one this can merge into. Never overwritten."""


def merge_metrics(path: Path, rows: list[dict], today: date) -> int:
    existing = {}
    if path.exists():
        # A file that is there but cannot be read as a metrics file is refused, not replaced:
        # writing over it would throw away every row in it without a word.
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            companies = body.get("companies", []) if isinstance(body, dict) else None
            if not isinstance(companies, list):
                raise ValueError("it has no list of companies")
            existing = {r["symbol"]: r for r in companies}
        except (OSError, ValueError, KeyError, TypeError) as err:
            raise Unmergeable(f"{path} is there but is not a metrics file this can merge into "
                              f"({err}); nothing was written. Move it aside and run again.") from None
    for row in rows:
        existing[row["symbol"]] = row
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"as_of": today.isoformat(), "currency": "INR",
                                "companies": sorted(existing.values(), key=lambda r: r["symbol"])},
                               indent=2) + "\n", encoding="utf-8")
    return len(existing)


def watchlist_symbols(path: Path) -> list[str]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):         # missing, unreadable, not UTF-8, not JSON
        return []
    rows = (body.get("hits") or body.get("symbols") or []) if isinstance(body, dict) else body
    out = []
    for row in rows if isinstance(rows, list) else []:
        symbol = row.get("symbol") if isinstance(row, dict) else row
        if isinstance(symbol, str) and symbol.strip() and symbol.strip().upper() not in out:
            out.append(symbol.strip().upper())
    return out


# ---------------------------------------------------------------- four years (--annual)

def share_count_rows(symbol: str, by_year: dict) -> list[dict]:
    """The share count, shown only where two independent sources report the SAME NUMBER.

    Exactly the same number, to the share. The accounts source counts shares issued and the
    exchange's shareholding filing counts shares held by shareholders, so where they match there
    is nothing left to be wrong about, and where they do not, no amount of arithmetic decides
    which to believe. A count that is not matched is not shown, and neither rival figure is
    printed: showing two would ask the reader to pick one, which is the same as publishing a guess.
    """
    years = sorted(by_year, reverse=True)
    counts, missing = shares.confirmed(symbol, years)
    try:
        corporate = nse.corporate_actions(symbol)
    except Exception:                                            # noqa: BLE001
        corporate = None          # unread, which is not the same as "no bonus or split"
    out = []
    for when in years:
        row: dict = {"year_end": when, "shares": counts.get(when), "refused": []}
        if when in missing:
            why = missing[when]
            if "do not report the same count" in why:
                why += _because(corporate, when)
            row["refused"].append(why)
        out.append(row)
    return out


def _because(corporate: list[dict], when: str) -> str:
    """Why the two sources differ, where a corporate action explains it. Explanation only."""
    if corporate is None:
        return (". The exchange's record of bonuses and splits could not be read, so whether one "
                "explains it is not known")
    try:
        factor, why = actions.changed_since(corporate, when)
    except actions.Uncertain as err:
        return f". A corporate action since this year end may have restated one of them: {err}"
    if factor != 1.0:
        return (f". The share basis changed after this year end ({'; '.join(why)}), which "
                f"restates one source and not the other")
    return (". No bonus or split since this year end explains it, so the two are counting "
            "different things: shares issued against shares held by shareholders")


def crore(value):
    return round(value / CRORE) if value is not None else None


def table_rows(by_year: dict) -> list[dict]:
    """The accounts in crore. A year not in rupees is blanked, not shown: its figures under a
    "crore" heading would read as rupees, eighty times off, and they are never converted."""
    out = []
    for when in sorted(by_year, reverse=True):
        v = by_year[when]["values"] if yahoo.in_rupees(by_year[when]) else {}
        out.append({
            "year_end": when,
            "revenue": crore(v.get("revenue")),
            "pbt": crore(v.get("profit_before_tax")),
            "profit": crore(v.get("net_profit_owners")),
            "assets": crore(v.get("assets")),
            "equity": crore(v.get("equity")),
            "debt": crore(v.get("debt")),
            "cash": crore(v.get("cash")),
            "cash_from_ops": crore(v.get("cash_from_operations")),
            "currency": by_year[when]["currency"],
        })
    return out


def ratios(by_year: dict) -> list[dict]:
    """Ratios these figures imply. Computed here, and labelled as computed.

    Return on equity uses the owners' profit against the owners' equity, because market value is a
    claim on the owners' share. Return on assets uses the whole group's profit against the whole
    group's assets, and only that: falling back to the owners' profit would mix the two, which is
    the most common way these come out wrong. A zero profit is a 0.0% return, not a blank; a
    denominator of zero or less (negative equity) makes the ratio meaningless and it is left out.
    """
    out = []
    for when in sorted(by_year, reverse=True):
        v = by_year[when]["values"]
        owners, group = v.get("net_profit_owners"), v.get("net_profit")
        equity, assets = v.get("equity"), v.get("assets")
        revenue, ebitda, debt = v.get("revenue"), v.get("ebitda"), v.get("debt")

        def ratio(part, whole, scale=100.0, places=1):
            if part is None or whole is None or whole <= 0:
                return None
            return round(part / whole * scale, places)

        out.append({"year_end": when,
                    "roe_percent": ratio(owners, equity),
                    "roa_percent": ratio(group, assets),
                    "margin_percent": ratio(owners, revenue),
                    "ebitda_margin_percent": ratio(ebitda, revenue),
                    "debt_to_equity": ratio(debt, equity, 1.0, 2)})
    return out


def contradictions(by_year: dict) -> list[str]:
    """Figures that cannot all be right, which is the only check most of these have."""
    found = []
    for when in sorted(by_year):
        v = by_year[when]["values"]
        if not yahoo.in_rupees(by_year[when]):
            found.append(f"{when}: reported in {by_year[when]['currency']}, not rupees, so it is "
                         f"not comparable with a rupee share price, is not converted and is not "
                         f"shown")
            continue
        assets, equity = v.get("assets"), v.get("equity")
        if assets is not None and equity is not None and equity > assets * 1.001:
            found.append(f"{when}: equity {crore(equity):,} is more than total assets "
                         f"{crore(assets):,}, which cannot be")
        revenue, pbt = v.get("revenue"), v.get("profit_before_tax")
        if revenue is not None and pbt is not None and revenue > 0 and pbt > revenue * 1.5:
            found.append(f"{when}: profit before tax {crore(pbt):,} is more than half again the "
                         f"revenue of {crore(revenue):,}; possible with large other income, but "
                         f"worth reading the accounts before using it")
        for key in ("assets", "revenue"):
            if v.get(key) is not None and v[key] < 0:
                found.append(f"{when}: {key} {crore(v[key]):,} is negative")
    return found


def annual(symbol: str, args, saved: dict) -> tuple[int, dict | None]:
    """One company's four years. Returns (exit code, what --json prints for it).

    Printing is done here for the table; with --json nothing is printed and the caller prints one
    document for every company, so the output stays one readable JSON value.
    """
    try:
        by_year = yahoo.accounts(symbol)
    except yahoo.NotReported as err:
        why = (f"{err}. Funds, trusts and some recent listings have no accounts here."
               if not isinstance(err, yahoo.Unreachable) else f"{err}")
        print(f"results: {symbol}: {why}", file=sys.stderr)
        return 2, None

    if args.save:
        saved[symbol] = by_year

    if args.shares:
        rows = share_count_rows(symbol, by_year)
        table = [{"year_end": r["year_end"], "shares": r["shares"],
                  "crore": round(r["shares"] / CRORE, 2) if r["shares"] else None}
                 for r in rows]
        if args.json:
            return 0, {"symbol": symbol, "shares": [dict(t, refused=r["refused"])
                                                    for t, r in zip(table, rows)]}
        print(f"{symbol}, share count where two independent sources report the same number.\n")
        show.emit(table, False, [("year_end", "year end", 11), ("shares", "shares", 16),
                                 ("crore", "crore", 10)], empty="Nothing reported.")
        notes = [(r["year_end"], why) for r in rows for why in r["refused"]]
        if notes:
            print("\n   NOT SHOWN, AND WHY:")
            for when, why in notes:
                print(f"      {when}: {why}")
        print("\n      A blank is a count that two sources did not agree on, never a zero "
              "and never one\n      source's guess.")
        return 0, None

    if args.ratios:
        rows = ratios(by_year)
        note = ("Computed here, not reported. ROE uses the owners' profit over the owners' "
                "equity; ROA uses the group's profit over the group's assets. Mixing those "
                "is the usual way these come out wrong.")
        if args.json:
            return 0, {"symbol": symbol, "ratios": rows, "note": note}
        print(f"{symbol}, ratios computed from the reported figures\n")
        show.emit(rows, False,
                  [("year_end", "year end", 11), ("roe_percent", "ROE%", 8),
                   ("roa_percent", "ROA%", 8), ("margin_percent", "margin%", 9),
                   ("ebitda_margin_percent", "EBITDA%", 9),
                   ("debt_to_equity", "debt/eq", 9)],
                  empty="Not enough reported to compute anything.", note=note)
        return 0, None

    rows = table_rows(by_year)
    trouble = contradictions(by_year)
    if args.json:
        return 0, {"symbol": symbol, "currency_unit": "INR crore", "years": rows,
                   "cannot_all_be_right": trouble}
    print(f"{symbol}, accounts as reported, in crore. {len(rows)} years, which is all this "
          f"source gives.\n")
    show.emit(rows, False,
              [("year_end", "year end", 11), ("revenue", "revenue", 10), ("pbt", "pbt", 9),
               ("profit", "profit", 9), ("assets", "assets", 11), ("equity", "equity", 10),
               ("debt", "debt", 9), ("cash", "cash", 9), ("cash_from_ops", "cash ops", 9)],
              empty="Nothing reported.")
    if trouble:
        print("\n   THESE FIGURES CANNOT ALL BE RIGHT:")
        for line in trouble:
            print(f"      {line}")
    print("\n      'profit' is the share belonging to the owners, not the whole group's. "
          "'cash' is the\n      closing balance from the cash flow statement. For "
          "per-share figures as filed, run\n      without --annual: the quarterly filings "
          "carry the company's own diluted EPS.")
    return 0, None


def run_annual(wanted: list[str], args, today: date) -> int:
    """--annual for every company asked for: one JSON document with --json, and --save writing
    every company to nse/results.json under its own symbol (one company's years with no name on
    them, overwritten by the next, is a file nobody can read correctly)."""
    if args.offline:
        print("Refused: --annual reads a live accounts source and there is no saved copy of it, "
              "so it cannot run with --offline.", file=sys.stderr)
        return 2
    saved: dict = {}
    codes, docs = [], []
    for symbol in wanted:
        code, doc = annual(symbol, args, saved)
        codes.append(code)
        if doc is not None:
            docs.append(doc)
    if args.save and saved:
        where = show.hand_off("results.json", {"as_of": today.isoformat(),
                                               "source": "keyless annual accounts feed",
                                               "companies": saved})
        print(f"wrote {where} ({len(saved)} companies)", file=sys.stderr if args.json else sys.stdout)
    if args.json:
        print(json.dumps(docs, indent=2, default=str))
    return 0 if all(c == 0 for c in codes) else (1 if 0 in codes else 2)


# ---------------------------------------------------------------- the command

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", help="NSE symbols, e.g. TCS SBIN (default: the "
                                               "watchlist)")
    ap.add_argument("--symbol", help="one symbol (the same as giving it positionally)")
    ap.add_argument("--quarters", type=int, default=None,
                    help="how many quarters to show (default 6, or results_quarters in config)")
    ap.add_argument("--basis", choices=("consolidated", "standalone"), default=None,
                    help="which statements (default consolidated, where filed)")
    ap.add_argument("--no-filings", action="store_true",
                    help="skip the board-meeting notices (next results date, data-lag note)")
    ap.add_argument("--metrics", nargs="?", const="nse/metrics.json", metavar="FILE",
                    help="merge a row per company into this JSON (default nse/metrics.json)")
    ap.add_argument("--watchlist", help="read the symbols from here (default nse/watchlist.json)")
    ap.add_argument("--config", help="your settings file (default config.json or nse/config.json)")
    ap.add_argument("--offline", metavar="DIR", help="saved exchange answers, no network")
    ap.add_argument("--now", help="pin the clock, like '2026-10-24 09:00' (IST)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--annual", action="store_true",
                    help="four years of accounts instead (with --ratios or --shares)")
    ap.add_argument("--ratios", action="store_true", help="--annual: the ratios they imply")
    ap.add_argument("--shares", action="store_true",
                    help="--annual: the share count, where two sources report the same number")
    ap.add_argument("--save", action="store_true", help="--annual: also write nse/results.json")
    args = ap.parse_args(argv)

    try:
        conf = settings.load(args.config, DEFAULTS)
    except FileNotFoundError as err:
        print(f"Refused: {err}", file=sys.stderr)
        return 2
    if args.quarters is not None:
        count = args.quarters
    else:
        setting = conf.get("results_quarters")
        setting = 6 if setting is None else setting
        if isinstance(setting, bool) or not isinstance(setting, int):
            print(f"Refused: results_quarters in your settings must be a whole number of "
                  f"quarters, like 6, not {setting!r}.", file=sys.stderr)
            return 2
        count = setting
    if count < 1:
        print("--quarters must be at least 1." if args.quarters is not None else
              "Refused: results_quarters in your settings must be at least 1.", file=sys.stderr)
        return 2
    basis = args.basis or conf.get("results_basis") or "consolidated"
    if basis not in ("consolidated", "standalone"):
        print(f"Refused: results_basis in your settings must be \"consolidated\" or "
              f"\"standalone\", not {basis!r}.", file=sys.stderr)
        return 2
    try:
        today = clock.now(args.now).date()
    except ValueError as err:
        print(f"Refused: {err}", file=sys.stderr)
        return 2

    wanted = [s.upper() for s in args.symbols]
    if args.symbol and args.symbol.upper() not in wanted:
        wanted.append(args.symbol.upper())
    if not wanted:
        path = Path(args.watchlist) if args.watchlist else Path("nse") / "watchlist.json"
        wanted = watchlist_symbols(path)
        if not wanted:
            ap.print_help()
            print(f"\nName a symbol, or put some on {path} with /nse-scan.")
            return 2

    if args.annual or args.ratios or args.shares or args.save:
        return run_annual(wanted, args, today)

    done, rows, failed = [], [], 0
    for symbol in wanted:
        try:
            found = one(symbol, count, basis, args.offline, args.no_filings, today)
        except (filings.NotFiled, nse.NotPublished) as err:
            said = str(err)
            print(f"results: {said if said.startswith(symbol) else f'{symbol}: {said}'}",
                  file=sys.stderr)
            failed += 1
            continue
        done.append(found)
        rows.append(metrics_row(found))
        if not args.json:
            print("\n".join(render(found, basis)))
            print()
    if args.json:
        print(json.dumps(done, indent=2, default=str))
    if args.metrics and rows:
        try:
            total = merge_metrics(Path(args.metrics), rows, today)
        except Unmergeable as err:
            print(f"Refused: {err}", file=sys.stderr)
            return 2
        if not args.json:
            print(f"wrote {args.metrics} ({total} companies)")
    if done and not args.json:
        print("Figures are what each company filed with the exchange. Nothing is estimated, and "
              "this says\nnothing about whether a stock is cheap or dear. Nothing was placed.")
    return 0 if done and not failed else (1 if done else 2)


if __name__ == "__main__":
    show.run(main)
