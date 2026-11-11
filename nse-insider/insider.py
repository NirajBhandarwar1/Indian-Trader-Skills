#!/usr/bin/env python3
"""Who is actually buying: insiders, institutions in bulk, and the daily foreign/domestic flow.

Three disclosures the exchange publishes and the US has no equal of:

  insider trades   filed under SEBI's insider trading (PIT) regulations: the person tells the
                   company within two trading days, and the company tells the exchange within two
                   trading days of being told -- once the person's trades in the quarter reach
                   Rs 10 lakh, so a filing can carry a trade weeks old. Each row names the person,
                   the mode, the side, the security, the shares and the value, and the category
                   where the filing gives one (many leave it as "-").
  bulk deals       any single party trading more than 0.5% of a company's shares in a day, naming
                   the client and the average price they paid; block deals, trades struck in the
                   exchange's separate block window, each leg naming its client (a leg can be
                   small: 0.09 crore in the saved SEDEMAC deals); short selling, the stock and
                   quantity sold short
  FII / DII flows  what foreign and domestic institutions bought and sold across the whole market

    python insider.py --insiders --symbol INFY --days 365
    python insider.py --insiders --new                 # your watchlist, plan and portfolio
    python insider.py --bulk --new --tickers SBIN ITC  # new bulk deals, yours flagged
    python insider.py --bulk --block --flows           # one section each
    python insider.py --flows

With --new only what was not shown at the last check appears, and every row in a stock you watch
or hold is flagged. Exit 0 when something is shown, 1 when nothing is (or nothing is new), 2 when
the question is refused or nothing asked for could be read. These are public disclosures, read
fast. They are days old by the time anyone can read them, and they are not inside information.

Needs no third-party packages and no account.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import clock, nse, show                           # noqa: E402
from nsetools.http import NotPublished                          # noqa: E402

CRORE = 1e7
# Rows remembered by --new are forgotten once their own date is this old. A row older than that is
# then never called new again: nothing filed or dealt that long ago can be news, and calling it new
# because it was forgotten would be false.
KEEP_DAYS = 400
# NSE symbols are capitals, digits, & and - (M&M, BAJAJ-AUTO): of the 2,359 in the exchange's equity
# list those are the only other characters. Anything else is a typo, a path or a Yahoo-style
# "INFY.NS" -- which the exchange would answer with an empty list, read as "no filings".
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&\-]{0,19}")

# A promoter buying in the open market is a different signal from an employee trust settling ESOPs,
# and the mode says which. Kept as a label rather than a score: ranking these would be inventing a
# judgement the filing does not support. OFF MARKET IS NOT HERE: it is a transfer outside the
# exchange -- in the real INFY filings, 15 of them, mostly the employee benefit trust passing
# shares to employees -- so nobody chose to trade at the market's price.
TELLING = {"market purchase", "market sale", "open market"}

# SEBI PIT Regulation 7(2): the insider discloses to the company within two trading days, and the
# company to the exchange within two trading days of receiving it. BUT THE CLOCK STARTS when a
# person's trades in a calendar quarter first add up to Rs 10 lakh, so a trade below that is
# disclosed with the one that crosses it, later, and on time. Measured: an INFY filing published
# 5 February 2026 carries a trade of 5 January and one of 2 February; the January trade looks a
# month late and almost certainly was not. A single row cannot show when its clock started, so no
# row is called late. The delays are shown as measured, and the reader can judge.


def rupees(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _text(value) -> str:
    """A field as trimmed text: None is blank, and a number in a saved file is not a crash."""
    return "" if value is None else str(value).strip()


def _whole(value: float | None):
    """A share count as a whole number when it is one, so 11535577.0 reads as 11535577."""
    return int(value) if value is not None and float(value).is_integer() else value


def day_of(raw: str) -> str:
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw).strip()[:17], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return str(raw)[:10]


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def _saved(offline: str, name: str):
    path = Path(offline) / name
    if not path.exists():
        raise NotPublished(f"nothing saved as {name} in {offline}")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise NotPublished(f"{path} could not be read ({err})") from None
    rows = body["data"] if isinstance(body, dict) and "_source" in body else body
    if isinstance(rows, dict) and isinstance(rows.get("data"), list):
        rows = rows["data"]
    if not isinstance(rows, list):
        raise NotPublished(f"{path} does not hold a list of rows")
    return rows


# ---------------------------------------------------------------- what one filing says

def _held(value) -> float | None:
    """A share count from the before/after columns. 'Nil' there is a holding of none: the real
    INFY row 570210 goes from Nil to 2,061 shares on an acquisition of 2,061."""
    if str(value).strip().lower() == "nil":
        return 0.0
    return rupees(value)


def side_of(row: dict) -> tuple[str, str]:
    """BUY, SELL or unknown, and where that came from. Never guessed.

    The filing's own transaction type first. Three real INFY rows leave it as "-", and for those
    the holding before and after says which way it went. Where neither does, it is "unknown":
    the mode is no guide, because 72 of INFY's ESOP rows are sales of ESOP shares.

    Two cases are "unknown" with the reason, never a pick: the type and the holding disagree
    (one of the two is wrong and a row cannot say which), and a type that is neither a buy nor a
    sale (a pledge, say). The holding is read only when the type is blank: beside a pledge, what
    moves may be the shares pledged, and reading that as a purchase would be false.
    """
    said = _text(row.get("tdpTransactionType")).upper()
    before, after = _held(row.get("befAcqSharesNo")), _held(row.get("afterAcqSharesNo"))
    moved = None
    if before is not None and after is not None and before != after:
        moved = "BUY" if after > before else "SELL"
    if said in ("BUY", "SELL"):
        if moved and moved != said:
            return "unknown", (f"the filing says {said} but the holding went from "
                               f"{_whole(before)} to {_whole(after)}")
        return said, "filing"
    if said not in ("", "-"):
        return "unknown", f"the filing's type is {_text(row.get('tdpTransactionType'))!r}"
    if moved:
        return moved, "holding before and after"
    return "unknown", ""


def derivative_of(row: dict) -> str | None:
    """A futures or options trade is not a share purchase. None when the filing says '-'."""
    parts = [_text(row.get(k)) for k in ("derivativeType", "tdpDerivativeContractType")]
    parts = [p for p in parts if p and p != "-"]
    return " ".join(parts) or None


def notional_of(row: dict) -> dict:
    """For a derivative, the filing's buy and sell notional value and units, AS FILED.

    The PIT form asks a derivative trade for these rather than for shares and a value. Every real
    INFY row is a share trade and carries "0" in them, so their unit was never seen in use: they
    are passed through unscaled, never turned into crore, and left out for a share trade.
    """
    if not derivative_of(row):
        return {"derivative_buy_value": None, "derivative_buy_units": None,
                "derivative_sell_value": None, "derivative_sell_units": None}
    return {"derivative_buy_value": _whole(rupees(row.get("buyValue"))),
            "derivative_buy_units": _whole(rupees(row.get("buyQuantity"))),
            "derivative_sell_value": _whole(rupees(row.get("sellValue"))),
            "derivative_sell_units": _whole(rupees(row.get("sellquantity")))}


# ---------------------------------------------------------------- how long it took to disclose

def trading_days_between(start: date, end: date) -> int | None:
    """Trading days after `start` up to and including `end`; None if a year's holidays are unknown.

    None is not zero. The exchange publishes only the current year's holiday list, so a filing
    from a year whose list is not known here cannot be judged in trading days, and saying "on time"
    or "late" about it would be a guess.
    """
    if end < start:
        return None
    count, day = 0, start
    try:
        while day < end:
            day += timedelta(days=1)
            if clock.is_trading_day(day, offline=True):
                count += 1
    except clock.Unknown:
        return None
    return count


def disclosure(traded: str | None, told: str | None, published: str | None) -> dict:
    """The disclosure timeline as measured: calendar days to publication, each leg in trading days.

    `traded` is the LAST day of the trade (acqtoDt). Never a verdict on whether it was late; see
    the note on Regulation 7(2) above. A publication dated before the trade is a filing whose
    dates contradict each other, and no delay is worked out from it.
    """
    t, k, p = _date(traded), _date(told), _date(published)
    days = (p - t).days if p and t else None
    return {"days_to_publish": days if days is None or days >= 0 else None,
            "to_company_trading_days": trading_days_between(t, k) if t and k else None,
            "to_exchange_trading_days": trading_days_between(k, p) if k and p else None}


# ---------------------------------------------------------------- the three feeds

def insiders(symbol: str, days: int, min_crore: float, today: date | None = None,
             offline: str | None = None, report: dict | None = None) -> list[dict]:
    """Insider and promoter dealings for one company.

    A SYMBOL IS REQUIRED. Asked market-wide the endpoint answers 200 with an empty list however
    wide the dates, which reads as "no insider activity anywhere" and is wrong -- INFY returns 197
    filings over a year. Measured 2026-10-23.

    `report`, if given, is filled with what was set aside and why: rows that could not be dated
    and rows naming another company. Nothing is dropped without being counted.
    """
    today = today or clock.now().date()
    report = report if report is not None else {}
    report.setdefault("undated", 0)
    report.setdefault("other_company", 0)
    rows = (_saved(offline, f"insider-{symbol.upper()}.json") if offline
            else nse.insider_trades(symbol, days, today))
    since = (today - timedelta(days=days)).isoformat()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            report["undated"] += 1
            continue
        if _text(row.get("symbol")) and _text(row["symbol"]).upper() != symbol.upper():
            report["other_company"] += 1
            continue
        value = rupees(row.get("secVal"))
        if value is not None and min_crore and value < min_crore * CRORE:
            continue
        filed = day_of(row.get("date") or "")
        if not _date(filed):
            report["undated"] += 1
            continue
        if not since <= filed <= today.isoformat():
            continue
        mode = _text(row.get("acqMode"))
        before = rupees(row.get("befAcqSharesPer"))
        after = rupees(row.get("afterAcqSharesPer"))
        start = day_of(row.get("acqfromDt") or row.get("acqtoDt") or "")
        end = day_of(row.get("acqtoDt") or row.get("acqfromDt") or "")
        start, end = (start if _date(start) else None), (end if _date(end) else None)
        told = day_of(row.get("intimDt") or "")
        side, side_from = side_of(row)
        # "did" is the transaction, "pid" the filing: one filing can carry many trades (INFY's 197
        # rows over a year are 27 filings, one of them 102 trades), so keying on pid would alert on
        # one trade and silently mark the rest as seen.
        ident = row.get("did") or hashlib.sha1(json.dumps(row, sort_keys=True).encode()).hexdigest()
        out.append({
            "id": f"pit:{ident}",
            "transaction_id": row.get("did"),
            "filing_id": row.get("pid"),
            "filed": filed,
            # One day, or "from to to" when the filing covers several. The delays below are
            # measured from the LAST of them.
            "traded": start if start == end else f"{start or '?'} to {end or '?'}",
            "traded_from": start,
            "traded_to": end,
            "symbol": (_text(row.get("symbol")) or symbol).upper(),
            "who": _text(row.get("acqName")),
            "category": _text(row.get("personCategory")),
            "mode": mode,
            "side": side,
            "side_from": side_from,
            "security": _text(row.get("secType")),
            "derivative": derivative_of(row),
            **notional_of(row),
            "shares": _whole(rupees(row.get("secAcq"))),
            "value_crore": round(value / CRORE, 3) if value is not None else None,
            "held_before": _whole(_held(row.get("befAcqSharesNo"))),
            "held_after": _whole(_held(row.get("afterAcqSharesNo"))),
            "holding_before_percent": before,
            "holding_after_percent": after,
            "moved_percent": round(after - before, 3)
                             if before is not None and after is not None else None,
            "telling": mode.lower() in TELLING,
            "told_company": told if _date(told) else None,
            "link": row.get("xbrl"),
            **disclosure(end, told, filed),
        })
    out.sort(key=lambda r: (r["filed"], r["value_crore"] or 0), reverse=True)
    return out


def _deal_rows(kind: str, days: int, today: date, offline: str | None) -> tuple[list, bool]:
    """The raw rows, and whether they may be cut short by the JSON answer's 70-row cap.

    Saved answers: for bulk and block deals `{kind}.csv` (the exchange's CSV export, complete) is
    read before `{kind}.json`; short selling is read only from `short_selling.json`, as live, since
    its CSV columns were never checked. Live: bulk and block deals come from the CSV export.
    """
    if offline:
        csv_path = Path(offline) / f"{kind}.csv"
        if kind in ("bulk_deals", "block_deals") and csv_path.exists():
            return nse.deals_from_csv(csv_path.read_bytes()), False
        rows = _saved(offline, f"{kind}.json")
        return rows, len(rows) >= nse.DEALS_JSON_CAP
    rows = nse.bulk_block_deals(kind, days, today)
    capped = kind not in ("bulk_deals", "block_deals") and len(rows) >= nse.DEALS_JSON_CAP
    return rows, capped


def deals(kind: str, days: int, symbol: str | None, min_crore: float,
          today: date | None = None, offline: str | None = None,
          report: dict | None = None) -> list[dict]:
    """Bulk deals, block deals or short selling, with the counterparty named where there is one.

    `report`, if given, says whether the answer may be cut short (`capped`) and how many rows
    could not be read (`unreadable`: no symbol, no date or no quantity). Short-selling rows carry
    no client, side or price, so their value is unknown, never zero.
    """
    today = today or clock.now().date()
    report = report if report is not None else {}
    rows, report["capped"] = _deal_rows(kind, days, today, offline)
    report["received"] = len(rows)
    report.setdefault("unreadable", 0)
    since = (today - timedelta(days=days)).isoformat()
    out = []
    # Two legs alike in every field (same day, stock, client, side, quantity and price) are two
    # deals. Keyed alike, --new would remember the second as seen when only the first was shown,
    # so each carries its place among its twins.
    twins: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            report["unreadable"] += 1
            continue
        short = "SS_SYMBOL" in row or "SS_DATE" in row
        sym = str(row.get("SS_SYMBOL" if short else "BD_SYMBOL") or "").strip().upper()
        when = day_of(row.get("SS_DATE" if short else "BD_DT_DATE") or "")
        qty = rupees(row.get("SS_QTY" if short else "BD_QTY_TRD"))
        if not sym or not _date(when) or qty is None:
            report["unreadable"] += 1
            continue
        if symbol and sym != symbol.upper():
            continue
        if not since <= when <= today.isoformat():
            continue
        price = None if short else rupees(row.get("BD_TP_WATP"))
        value = qty * price if price is not None else None
        if min_crore and value is not None and value < min_crore * CRORE:
            continue
        client = "" if short else str(row.get("BD_CLIENT_NAME") or "").strip()
        side = "" if short else str(row.get("BD_BUY_SELL") or "").strip().upper()
        key = f"{kind}|{when}|{sym}|{client}|{side}|{qty}|{price}"
        twins[key] = twins.get(key, 0) + 1
        if twins[key] > 1:
            key += f"|{twins[key]}"
        ident = hashlib.sha1(key.encode()).hexdigest()[:20]
        out.append({
            "id": f"{kind}:{ident}",
            "date": when,
            "symbol": sym,
            "company": str(row.get("SS_NAME" if short else "BD_SCRIP_NAME") or "").strip(),
            "client": client,
            "side": side,
            "shares": _whole(qty),
            "price": price,
            "value_crore": round(value / CRORE, 2) if value is not None else None,
        })
    mark_round_trips(out)
    out.sort(key=lambda r: (r["date"], r["value_crore"] if r["value_crore"] is not None else -1),
             reverse=True)
    return out


def mark_round_trips(rows: list[dict]) -> None:
    """Flag a client who both bought and sold the same stock on the same day.

    This is the trap in bulk deal data and it is not small. In the real 24 September 2026 answer,
    50 of the 70 bulk deals are a client on both sides of one stock: MICROCURVES bought and sold
    313,241 GNA, at 630.87 and 631.41. A desk passing stock through itself crosses the 0.5%
    disclosure threshold on both legs and shows up twice, so a naive reading sees deals of real
    interest where most of the list is one firm's inventory going round.

    Nothing is dropped, because the deal did happen and the price is real. It is labelled, so a
    reader can tell a position being built from a book being turned.
    """
    by_day: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in rows:
        if not row.get("client") or row.get("side") not in ("BUY", "SELL"):
            continue
        key = (row["date"], row["symbol"], row["client"])
        by_day.setdefault(key, {})[row["side"]] = (
            by_day.setdefault(key, {}).get(row["side"], 0.0) + (row["shares"] or 0.0))

    for row in rows:
        sides = by_day.get((row["date"], row["symbol"], row.get("client")), {})
        bought, sold = sides.get("BUY", 0.0), sides.get("SELL", 0.0)
        both = bought > 0 and sold > 0
        # BOTH SIDES is the signal, not how flat the legs are. A 10% band would let LAXMI TRADE
        # SOLUTIONS through on HEROMOTORS that same day -- 2,226,406 bought and 2,552,391 sold,
        # 13% apart, which reads as a real position and is a desk working an order. Any client on
        # both sides of the same stock on the same day is turning stock, whatever the residue.
        row["round_trip"] = bool(both)
        row["net_shares"] = _whole(round(bought - sold, 0)) if both else row["shares"]
        if not both:
            row["note"] = ""
        elif abs(bought - sold) <= max(bought, sold) * 0.1:
            row["note"] = "both sides, flat by the close"
        else:
            row["note"] = f"both sides, net {bought - sold:+,.0f}"


def flows(offline: str | None = None, report: dict | None = None) -> list[dict]:
    """What foreign and domestic institutions did across the market, in crore."""
    report = report if report is not None else {}
    report.setdefault("unreadable", 0)
    out = []
    for row in (_saved(offline, "fii-dii.json") if offline else nse.fii_dii_flows()):
        when = day_of(row.get("date") or "") if isinstance(row, dict) else ""
        if not _date(when):
            report["unreadable"] += 1
            continue
        who = _text(row.get("category"))
        out.append({
            "id": f"flow:{when}|{who}",
            "date": when,
            "who": who,
            "symbol": "",
            "bought_crore": rupees(row.get("buyValue")),
            "sold_crore": rupees(row.get("sellValue")),
            "net_crore": rupees(row.get("netValue")),
        })
    return out


# ---------------------------------------------------------------- yours, and what is new

def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def watch_from_files(portfolio: Path, watchlist: Path, plan: Path,
                     unread: list[str] | None = None,
                     odd: list[str] | None = None) -> list[str]:
    """What you hold, then the planned trade, then the scanner's hits.

    A file that exists but cannot be read is named in `unread`, so its stocks going unflagged is
    said rather than silent. A position is read by `symbol`, `tradingsymbol` (the broker's name for
    it) or `ticker`, as /nse-risk reads it. A name that is not an NSE symbol ("INFY.NS") is put in
    `odd` and not watched: asked about, the exchange would answer "no filings".
    """
    out: list[str] = []

    def add(symbol):
        symbol = (symbol or "").strip().upper() if isinstance(symbol, str) else ""
        if not symbol:
            return
        if not SYMBOL.fullmatch(symbol):
            if odd is not None and symbol not in odd:
                odd.append(symbol)
            return
        if symbol not in out:
            out.append(symbol)

    def load(path):
        saved = _read(path)
        if saved is None and path.exists() and unread is not None:
            unread.append(str(path))
        return saved

    saved = load(portfolio) or {}
    for row in (saved.get("positions") or []) if isinstance(saved, dict) else []:
        if isinstance(row, dict):
            add(row.get("symbol") or row.get("tradingsymbol") or row.get("ticker"))
    saved = load(plan) or {}
    if isinstance(saved, dict):
        add(saved.get("symbol"))
    saved = load(watchlist) or {}
    hits = saved.get("hits") if isinstance(saved, dict) else saved
    for hit in hits if isinstance(hits, list) else []:
        add(hit.get("symbol") if isinstance(hit, dict) else hit)
    return out


def _row_day(row: dict) -> str:
    return row.get("filed") or row.get("date") or ""


def fresh(rows: list[dict], state: dict, feed: str) -> list[dict]:
    """Rows not shown at an earlier check. The first check shows the whole window.

    A row dated before the feed's `forgot_before` was possibly shown and then forgotten, so it is
    never called new.
    """
    seen = state.get("seen", {}).get(feed, {})
    forgot = state.get("forgot_before", {}).get(feed) or ""
    return [r for r in rows if r["id"] not in seen and _row_day(r) >= forgot]


def remember(rows: list[dict], state: dict, feed: str, today: date, now: datetime) -> None:
    """Mark these rows as shown, keyed by id with the row's own date, and forget the old ones."""
    seen = state.setdefault("seen", {}).setdefault(feed, {})
    for row in rows:
        seen.setdefault(row["id"], _row_day(row))
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    kept = {k: v for k, v in seen.items() if str(v)[:10] >= cutoff}
    if len(kept) < len(seen):
        forgot = state.setdefault("forgot_before", {})
        forgot[feed] = max(forgot.get(feed) or "", cutoff)
    state["seen"][feed] = kept
    stamp = now.isoformat(timespec="seconds")
    state.setdefault("checked", {})[feed] = stamp
    state["last_check"] = stamp


def _state_problem(state) -> str | None:
    if not isinstance(state, dict):
        return f"it holds a {type(state).__name__}, not the object this skill writes"
    for key in ("seen", "checked", "forgot_before"):
        part = state.get(key, {})
        # "seen" holds one object per feed; "checked" and "forgot_before" one date text per feed.
        # A number there would be compared with a date and stop the run halfway.
        want = dict if key == "seen" else str
        if not isinstance(part, dict) or not all(isinstance(v, want) for v in part.values()):
            return f"its '{key}' is not in the shape this skill writes"
    if not isinstance(state.get("last_check"), (str, type(None))):
        return "its 'last_check' is not in the shape this skill writes"
    return None


def member_words(name: str) -> list[str]:
    """--member as the words to find, in any order: 'Atul Chaturvedi', 'Chaturvedi, Atul' and
    'chaturvedi' all find the filing by Atul Chaturvedi. The US pack takes the same three forms."""
    name = (name or "").replace(",", " ").lower()
    return name.split()


def mark_yours(rows: list[dict], yours: set[str]) -> None:
    for row in rows:
        row["yours"] = "<- yours" if row.get("symbol") in yours else ""


# ---------------------------------------------------------------- the command

INSIDER_NOTE = (
    "An ESOP settlement is not a promoter buying in the market: read the mode, and the side -- "
    "an ESOP row can be a sale. 'side' is the filing's own transaction type; where it is blank, "
    "the holding before and after; 'unknown' where neither says, where the two disagree, or where "
    "the type is neither a buy nor a sale (the JSON's side_from says which). 'cr' is the filing's "
    "value in "
    "rupees, in crore. 'days' is calendar days from the "
    "LAST day of the trade to the exchange; 'co' and 'nse' are trading days to the company and "
    "then to the exchange, blank where the year's holiday list is not known. None is called late: "
    "the two-day clock starts when a quarter's trades reach Rs 10 lakh, which one row cannot "
    "show. Public filings, not inside information.")
PUBLIC = " Public disclosures, read fast; not inside information."


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", help="one company")
    ap.add_argument("--tickers", nargs="*", help="these companies (insiders), or flag these "
                                                  "(deals). Default: portfolio, plan, watchlist")
    ap.add_argument("--insiders", action="store_true", help="PIT filings for a company")
    ap.add_argument("--bulk", action="store_true", help="bulk deals, over 0.5%% of a company in a day")
    ap.add_argument("--block", action="store_true", help="block deals, single negotiated trades")
    ap.add_argument("--short", action="store_true", help="short selling disclosures")
    ap.add_argument("--flows", action="store_true", help="foreign and domestic institutional flow")
    ap.add_argument("--new", action="store_true",
                    help="only what was not shown at the last check (exit 1 when nothing is new)")
    ap.add_argument("--days", type=int, default=30, help="how far back (default 30, at least 1)")
    ap.add_argument("--since", help="only filings on or after YYYY-MM-DD")
    ap.add_argument("--year", type=int, help="one calendar year of filings, like 2026")
    ap.add_argument("--member", "--who", dest="member", action="append",
                    help="only this person or client (part of the name; repeat for more)")
    ap.add_argument("--type", "--mode", dest="mode", action="append",
                    help="insiders: only this mode, like 'Market Purchase' or ESOP (repeatable)")
    ap.add_argument("--today", help="pin the date, YYYY-MM-DD, for a reproducible run")
    ap.add_argument("--min-crore", type=show.number, default=0.0, help="ignore anything smaller")
    ap.add_argument("--real-only", action="store_true",
                    help="hide deals where the same client was on both sides that day")
    ap.add_argument("--top", "--limit", dest="top", type=int, default=30,
                    help="at most this many rows per section (default 30, at least 1)")
    ap.add_argument("--portfolio", default="nse/portfolio.json")
    ap.add_argument("--watchlist", default="nse/watchlist.json")
    ap.add_argument("--plan", default="nse/plan.json")
    ap.add_argument("--state", default="nse/state/insider.json")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--offline", metavar="DIR", help="saved exchange answers, no network")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true", help="also write nse/insider.json")
    args = ap.parse_args(argv)

    def refuse(message: str) -> int:
        print(message, file=sys.stderr if args.json else sys.stdout)
        return 2

    asked_flags = [args.insiders, args.bulk, args.block, args.short, args.flows]
    if not any(asked_flags):
        ap.print_help()
        print("\nAsk for at least one of --insiders, --bulk, --block, --short or --flows.")
        return 2

    # ------------------------------------------------------------ the window, checked first
    try:
        today = date.fromisoformat(args.today) if args.today else clock.now().date()
    except ValueError:
        return refuse(f"--today wants a date like 2026-10-24, got {args.today!r}")
    if args.days < 1:
        return refuse(f"--days wants 1 or more, got {args.days}")
    if args.top < 1:
        return refuse(f"--top wants 1 or more, got {args.top}")
    if args.min_crore < 0:
        return refuse(f"--min-crore wants 0 or more, got {args.min_crore:g}")
    try:
        end, start, ranged = today, today - timedelta(days=args.days), False
    except OverflowError:
        return refuse(f"--days {args.days} reaches back before the calendar does")
    if args.year is not None:
        if not 1 <= args.year <= 9999:
            return refuse(f"--year wants a year like 2026, got {args.year}")
        start, end, ranged = date(args.year, 1, 1), min(today, date(args.year, 12, 31)), True
        if start > today:
            return refuse(f"--year {args.year} has not started by {today}")
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            return refuse(f"--since wants a date like 2026-10-01, got {args.since!r}")
        start, ranged = (max(start, since) if args.year is not None else since), True
    if start > end:
        return refuse(f"--since {start} is after the end of the window, {end}")
    days = (end - start).days
    window = f"{start} to {end}"
    label = window if ranged else f"last {args.days} days ({window})"

    # ------------------------------------------------------------ who is asked about
    named: list[str] = []
    for raw in ([args.symbol] if args.symbol else []) + list(args.tickers or []):
        sym = raw.strip().upper()
        if not SYMBOL.fullmatch(sym):
            return refuse(f"{raw!r} is not an NSE symbol (capitals, digits, & and -, like M&M "
                          f"or BAJAJ-AUTO; no exchange suffix such as .NS)")
        if sym not in named:
            named.append(sym)
    unread: list[str] = []
    odd: list[str] = []
    files = watch_from_files(Path(args.portfolio), Path(args.watchlist), Path(args.plan), unread,
                             odd)
    insider_symbols = named or (files if args.new else [])
    if args.insiders and not insider_symbols:
        return refuse("--insiders needs --symbol or --tickers (or, with --new, a portfolio, plan "
                      "or watchlist naming a stock). Asked market-wide the exchange returns an "
                      "empty list however wide the dates, which looks like no activity and is not."
                      + "".join(f"\n  {p} exists but could not be read." for p in unread)
                      + "".join(f"\n  {s} in your files is not an NSE symbol." for s in odd))

    # A flag means you hold or watch it, or, for deals, that you named it with --tickers. Asking
    # about a company with --symbol does not make it yours, in either feed.
    yours_insider = set(files)
    deal_symbol = named[0] if args.symbol else None
    yours_deals = set(files) | {t.strip().upper() for t in (args.tickers or [])}

    state_path = Path(args.state)
    state: dict = {}
    if args.new and state_path.exists():
        state = _read(state_path)
        problem = ("it is not valid JSON" if state is None else _state_problem(state))
        if problem:
            return refuse(f"The state file {state_path} cannot be used: {problem}. Fix or remove "
                          f"it; reading it as empty would show everything again as new.")
    now = datetime.now(clock.IST)
    out_json: dict[str, list] = {}
    remembered = []

    def say(text: str = "") -> None:
        """Prose goes to stdout, except in --json mode, where stdout is only the JSON."""
        print(text, file=sys.stderr if args.json else sys.stdout)

    def text(line: str = "") -> None:
        if not args.json:
            print(line)

    for path in unread:
        say(f"{path} exists but could not be read, so its stocks are not flagged as yours.")
    for sym in odd:
        say(f"{sym} in your portfolio, plan or watchlist is not an NSE symbol (capitals, digits, & "
            f"and -), so it is not watched or flagged.")

    def narrow(rows, feed):
        """--member, --type and --min-crore: the filters, as opposed to the window. Returns the
        rows that pass, which filters were used, and which do not apply to this feed."""
        used, ignored = [], []
        # A blank --member or --type is no filter, as the US pack's --type '' lists every type;
        # read as a filter it would match nothing and say "nothing matched".
        wanted = [w for w in (member_words(m) for m in (args.member or [])) if w]
        modes = {m.strip().lower() for m in (args.mode or []) if m.strip()}
        if wanted:
            if feed in ("insider", "bulk_deals", "block_deals"):
                key = "who" if feed == "insider" else "client"
                rows = [r for r in rows
                        if any(all(w in r[key].lower() for w in words) for words in wanted)]
                used.append("--member")
            else:
                ignored.append("--member")
        if modes:
            if feed == "insider":
                rows = [r for r in rows if r["mode"].lower() in modes]
                used.append("--type")
            else:
                ignored.append("--type")
        if args.real_only and feed not in ("bulk_deals", "block_deals"):
            ignored.append("--real-only")
        if args.min_crore:
            if feed in ("insider", "bulk_deals", "block_deals"):
                rows = [r for r in rows
                        if r["value_crore"] is None or r["value_crore"] >= args.min_crore]
                used.append("--min-crore")
            else:
                ignored.append("--min-crore")
        return rows, used, ignored

    def only_new(rows, feed):
        return fresh(rows, state, feed) if args.new else rows

    def keep(feed, displayed):
        """Remember what was put in front of the reader, and only that: a row cut off by --top
        or hidden by --real-only was not shown, so it stays new for the next check."""
        if args.new and not args.dry_run:
            remember(displayed, state, feed, today, now)
            remembered.append(feed)

    def counts(used, new):
        return ((f", {used} match your filters" if used is not None else "")
                + (f", {new} new since the last check" if args.new else ""))

    def empty_text(feed, found, matched, nothing, used):
        if not found:
            return nothing
        if not matched:
            return (f"Nothing matched your filters ({', '.join(used)}): {found} found before "
                    f"them.")
        last = state.get("checked", {}).get(feed) or state.get("last_check")
        return f"Nothing new since {'the last check, ' + last if last else 'the last check'}."

    def ignored_text(ignored, why):
        return (f"{' and '.join(ignored)} {'does' if len(ignored) == 1 else 'do'} not apply to "
                f"{why} and {'was' if len(ignored) == 1 else 'were'} not used."
                if ignored else "")

    def cut(visible, displayed, title):
        more = len(visible) - len(displayed)
        if more > 0:
            # Named in --json, where no header says which section the line belongs to.
            say(f"\n{title + ': ' if args.json else ''}{more} more not shown (--top {args.top})"
                + ("; they stay new for the next check." if args.new else "."))

    def named_in_json(title, line):
        return f"{title}: {line}" if args.json else line

    # ------------------------------------------------------------ each feed, one section each
    def run_insiders() -> int:
        rows, problems, answered = [], [], []
        report = {"undated": 0, "other_company": 0}
        for symbol in insider_symbols:
            try:
                rows += insiders(symbol, days, 0, end, args.offline, report)
                answered.append(symbol)
            except NotPublished as err:
                problems.append(f"{symbol}: {err}")
        if not answered:
            say("Insider filings could not be read for any company asked, so nothing is reported "
                "as new or empty:")
            for problem in problems:
                say(f"  {problem}")
            return 2
        rows.sort(key=lambda r: (r["filed"], r["value_crore"] or 0), reverse=True)
        mark_yours(rows, yours_insider)
        matched, used, ignored = narrow(rows, "insider")
        shown = only_new(matched, "insider")
        displayed = shown[:args.top]
        keep("insider", displayed)
        out_json["insider"] = shown
        text(f"{', '.join(answered)} insider and promoter filings, {label}. {len(rows)} found"
             + counts(len(matched) if used else None, len(shown)) + ".")
        title = "Insider filings"
        for problem in problems:
            say(named_in_json(title, f"not read, so nothing here is about it: {problem}"))
        if report["undated"]:
            say(named_in_json(title, f"{report['undated']} filing rows carried no readable date "
                                     f"and are not shown."))
        if report["other_company"]:
            say(named_in_json(title, f"{report['other_company']} rows named another company and "
                                     f"are not shown."))
        if ignored:
            say(named_in_json(title, ignored_text(ignored, "insider filings")))
        text()
        if not args.json:
            columns = [("filed", "filed", 11),
                       ("traded", "traded", max([11] + [len(r["traded"] or "") for r in displayed])),
                       ("symbol", "symbol", 10), ("who", "who", 24),
                       ("category", "category", 10), ("mode", "mode", 12), ("side", "side", 7),
                       ("security", "security", 13)]
            if any(r["derivative"] for r in displayed):
                columns.append(("derivative", "derivative", 16))
            columns += [("shares", "shares", 10), ("value_crore", "cr", 8),
                        ("moved_percent", "moved%", 7),
                        ("days_to_publish", "days", 5), ("to_company_trading_days", "co", 3),
                        ("to_exchange_trading_days", "nse", 4), ("yours", "", 8)]
            show.emit(displayed, False, columns,
                      empty=empty_text("insider", len(rows), len(matched),
                                       f"No filings for {', '.join(answered)}, {window}. The "
                                       f"exchange answers the same for a symbol it does not "
                                       f"list, so check the spelling; or try a wider --days.",
                                       used),
                      note=INSIDER_NOTE)
            if displayed:
                print("\nThe filings:")
                for r in displayed:
                    print(f"  {r['filed']}  {r['who']}: transaction "
                          f"{r['transaction_id'] or '?'}, filing {r['filing_id'] or '?'}, "
                          f"{r['link'] or 'no link given'}")
        cut(shown, displayed, title)
        if displayed:
            return 0
        return 2 if problems else 1

    def run_deals(kind: str, title: str) -> int:
        report: dict = {}
        try:
            rows = deals(kind, days, deal_symbol, 0, end, args.offline, report)
        except NotPublished as err:
            say(f"{title}: the exchange gave nothing: {err}")
            return 2
        short = kind == "short_selling"
        mark_yours(rows, yours_deals)
        matched, used, ignored = narrow(rows, kind)
        shown = only_new(matched, kind)
        churn = sum(1 for r in shown if r.get("round_trip"))
        visible = [r for r in shown if not r.get("round_trip")] if args.real_only else shown
        displayed = visible[:args.top]
        keep(kind, displayed)
        out_json[kind] = visible
        mine = sum(1 for r in visible if r["yours"])
        where = f" in {deal_symbol}" if deal_symbol else ""
        found = f"At least {len(rows)}" if report["capped"] else str(len(rows))
        text(f"{title}{where}, {label}. {found} disclosed"
             + counts(len(matched) if used else None, len(shown))
             + (f", {churn} two-sided hidden, {len(visible)} shown" if args.real_only and churn
                else f", {churn} of them two-sided" if churn else "")
             + (f", {mine} in your stocks" if mine else "") + ".")
        if report["capped"]:
            say(named_in_json(title, f"The exchange's JSON answer stops at {nse.DEALS_JSON_CAP} "
                                     f"rows and this one has {report['received']}, so more were "
                                     f"probably disclosed than are listed here, and the latest "
                                     f"days may be the ones missing."))
        if report["unreadable"]:
            say(named_in_json(title, f"{report['unreadable']} rows had no readable symbol, date "
                                     f"or quantity and are not shown."))
        if ignored:
            say(named_in_json(title, ignored_text(
                ignored, "short selling, which names no client and no price" if short
                else "deals")))
        text()
        if not args.json:
            nothing = empty_text(kind, len(rows), len(matched),
                                 f"Nothing disclosed{where}, {window}.", used)
            if shown and not visible:
                nothing = f"All {len(shown)} were two-sided, and --real-only hides those."
            if short:
                columns = [("date", "date", 11), ("symbol", "symbol", 12),
                           ("company", "company", 32), ("shares", "shares sold short", 17),
                           ("yours", "", 8)]
                note = ("A short-selling disclosure names the stock and the quantity sold short "
                        "that day, not who sold it or at what price." + PUBLIC)
            else:
                columns = [("date", "date", 11), ("symbol", "symbol", 12), ("side", "side", 5),
                           ("client", "who", 30), ("shares", "shares", 11),
                           ("price", "price", 9), ("value_crore", "cr", 7),
                           ("note", "note", 30), ("yours", "", 8)]
                note = (("A client on both sides of the same stock on the same day is turning "
                         "stock, not building a position. --real-only drops them." if churn else
                         "A named client is one decision. The same volume spread over a "
                         "thousand accounts is not.") + PUBLIC)
            # The exchange's page for these, as the US pack gives each row its filing's link:
            # a deal has no page of its own, so the report it is listed in is named instead.
            show.emit(displayed, False, columns, empty=nothing,
                      note=note + f" Listed at {nse.PAGES['deals']}")
        cut(visible, displayed, title)
        return 0 if displayed else 1

    def run_flows() -> int:
        report: dict = {}
        try:
            rows = flows(args.offline, report)
        except NotPublished as err:
            say(f"Institutional flow: the exchange gave nothing: {err}")
            return 2
        inside = [r for r in rows if start.isoformat() <= r["date"] <= end.isoformat()]
        _, _, ignored = narrow([], "flows")
        if args.symbol or args.tickers:
            ignored.insert(0, "--symbol" if args.symbol else "--tickers")
        shown = only_new(inside, "flows")
        displayed = shown[:args.top]
        keep("flows", displayed)
        out_json["flows"] = shown
        days_in = sorted({r["date"] for r in inside})
        text("Institutional flow across the whole market, in crore, "
             + (f"for {', '.join(days_in)}" if days_in else label)
             + (f", {len(shown)} new since the last check" if args.new else "") + ".")
        title = "Institutional flow"
        if report["unreadable"]:
            say(named_in_json(title, f"{report['unreadable']} rows had no readable date and are "
                                     f"not shown."))
        if ignored:
            say(named_in_json(title, ignored_text(ignored, "the market-wide flow")))
        text()
        if not args.json:
            outside = sorted({r["date"] for r in rows} - set(days_in))
            if not rows:
                nothing = "The exchange's answer held no flow rows."
            elif not inside:
                nothing = (f"The exchange's flow is for {', '.join(outside)}, outside {window}, "
                           f"so none is shown as if it were.")
            else:
                nothing = empty_text("flows", len(inside), len(inside), "", [])
            show.emit(displayed, False,
                      [("date", "date", 11), ("who", "who", 10), ("bought_crore", "bought", 12),
                       ("sold_crore", "sold", 12), ("net_crore", "net", 12)],
                      empty=nothing,
                      note="A market-wide figure says nothing about any one stock. Listed at "
                           f"{nse.PAGES['flows']}")
        cut(shown, displayed, title)
        return 0 if displayed else 1

    asked = [run for flag, run in (
        (args.insiders, run_insiders),
        (args.bulk, lambda: run_deals("bulk_deals", "Bulk deals")),
        (args.block, lambda: run_deals("block_deals", "Block deals")),
        (args.short, lambda: run_deals("short_selling", "Short selling")),
        (args.flows, run_flows)) if flag]

    codes = []
    for i, run in enumerate(asked):
        if i:
            text("\n" + "=" * 72 + "\n")
        try:
            codes.append(run())
        except NotPublished as err:
            say(f"The exchange gave nothing: {err}")
            codes.append(2)

    if remembered:
        # Whole or not at all: a run stopped halfway through writing must not leave a state file
        # that the next check refuses, or worse, half of what was seen.
        show.hand_off(state_path.name, state, state_path)

    # One section asked for: its rows, as before. Several: one key per section.
    # --save keeps every row; --json, like the table, shows at most --top per section.
    def shape(by_feed):
        return next(iter(by_feed.values())) if len(asked) == 1 and by_feed else by_feed
    if args.save and out_json:
        print(f"wrote {show.hand_off('insider.json', shape(out_json))}", file=sys.stderr)
    if args.json and (out_json or len(asked) > 1):
        print(json.dumps(shape({k: v[:args.top] for k, v in out_json.items()}),
                         indent=2, default=str))

    # 0 when any section showed something. Otherwise 2 if a section could not be read -- an
    # unread feed must never pass for "nothing new" -- and 1 when everything read was empty.
    if 0 in codes:
        return 0
    return 2 if 2 in codes else 1


if __name__ == "__main__":
    show.run(main)
