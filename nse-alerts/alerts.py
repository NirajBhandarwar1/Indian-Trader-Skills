#!/usr/bin/env python3
"""Price Alerts for NSE stocks. Rings when a price, a stop or the opening bell hits.

Turns the trade plan into three alerts in one go (the stop, the target and flat-by), plus above /
below / percent-move / time-of-day / opening-bell rules. Knows the NSE clock and fetches free
delayed quotes with no key. An alert is information, never an order: nothing here sells anything.

    python alerts.py clock                       # NSE open or closed, next open
    python alerts.py add --from-plan             # stop, target and flat-by from nse/plan.json
    python alerts.py add --opening-bell
    python alerts.py add --symbol SBIN --stop 950.05
    python alerts.py add --symbol SBIN --target 985.45 --rearm --note "first target"
    python alerts.py add --at 15:20 --label "square off" --symbol SBIN
    python alerts.py add --symbol ITC --pct 3 --direction up --date 2026-10-22
    python alerts.py check                       # exit 0 = something rang; writes nse/prices.json
    python alerts.py quote SBIN ITC --write      # merge into nse/prices.json
    python alerts.py quote --holdings --write    # the plan and the portfolio
    python alerts.py list
    python alerts.py reset sbin-plan-stop

How each rule rings, the same as the US pack's:

    above / target   price at or above it    once, until `reset` -- or, with "rearm": true, again
    below / stop     price at or below it    after the price has moved back across the level
    pct_move         moved pct from the previous close (or "ref"), optional direction; once a day
    time             the IST clock passed "at", on a trading day only; once a trading day
    opening_bell     the NSE is open; once a trading day

A rule with a "date" is watched on that date only, so yesterday's stop never rings today. A price
rule rings only on a quote dated today; an older one is set aside and said.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date, datetime
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import clock, settings, show                       # noqa: E402
from nsetools.quotes import NoQuote, from_exchange, quote, quotes_for   # noqa: E402,F401

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "time", "opening_bell"}
NO_QUOTE = {"time", "opening_bell"}
FLAT_BY = "15:20"


class AlertError(Exception):
    """A rule or a command that cannot be acted on. Said plainly, never worked round."""


def write_prices(path: Path, quotes: dict, at: datetime, source: str) -> dict:
    """MERGE quotes into the prices file, each with its own quote time.

    Merged, not replaced: writing only what this run quoted would wipe every other price in the
    file, and /nse-budget and /nse-risk would lose them without a word. Each price keeps the time
    the quote itself carries, so a reader can tell a fresh price from a stale one.
    """
    old = _read(path, {}, lenient=True)
    old = old if isinstance(old, dict) else {}
    # A damaged file's parts are rebuilt rather than crashed on: it is a cache.
    prices = dict(old["prices"]) if isinstance(old.get("prices"), dict) else {}
    detail = dict(old["detail"]) if isinstance(old.get("detail"), dict) else {}
    stamp = at.replace(microsecond=0).isoformat()
    for symbol, q in quotes.items():
        prices[symbol] = q["price"]
        detail[symbol] = {"prev_close": q.get("previous_close"), "at": q.get("at"),
                          "source": q.get("source"), "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes for alerts and sizing checks. Your broker's price is the one "
                    "that fills. detail.at is each quote's own time."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


# ---- files -------------------------------------------------------------------------------------

def _read(path: Path, default, lenient: bool = False):
    """A JSON file, or `default` when it does not exist.

    A file that exists but does not parse is refused, as the US pack refuses it. Read as empty, a
    damaged rules file was overwritten by the next `add` (its rules lost) and a damaged state file
    made every rule that had rung ring again. Only the prices cache is lenient: it is rebuilt."""
    if path is None or not Path(path).exists():
        return default
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as err:
        if lenient:
            return default
        what = "cannot be read" if isinstance(err, OSError) else "is not valid JSON"
        raise AlertError(f"{path} {what} ({err}); fix or remove it") from None


def _write(path: Path, doc) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def _rules(path: Path) -> dict:
    """The rules file, {"rules": [...]}. An earlier version wrote a bare list, which still reads.

    Any other shape is refused, as a damaged file is: read as empty, the next `add` would write
    over the user's rules, and a rule that is not an object crashed the check."""
    doc = _read(path, {"rules": []})
    if isinstance(doc, list):
        doc = {"rules": doc}
    if not isinstance(doc, dict):
        raise AlertError(f"{path} must hold {{\"rules\": [...]}}, not a {type(doc).__name__}; "
                         f"fix or remove it")
    doc.setdefault("rules", [])
    if not isinstance(doc["rules"], list) or \
            not all(isinstance(r, dict) for r in doc["rules"]):
        raise AlertError(f"{path}: \"rules\" must be a list of rule objects; fix or remove it")
    return doc


def _state(path: Path) -> dict:
    """{"rung": {id: when}}. Any other shape is refused: an empty list read as "nothing has rung"
    made every rule that had rung ring again."""
    state = _read(path, {})
    if not isinstance(state, dict) or not isinstance(state.get("rung", {}), dict):
        raise AlertError(f"{path} must hold {{\"rung\": {{id: when}}}}; fix or remove it")
    return state


# ---- the rules ---------------------------------------------------------------------------------

def _hhmm(text):
    match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(text or "").strip())
    if not match:
        return None
    from datetime import time as clock_time
    return clock_time(int(match.group(1)), int(match.group(2)))


def _finite(value) -> bool:
    """A real number: not a bool (true is 1 to Python), not nan, not infinity."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _real_date(text) -> bool:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(text)):
        return False
    try:
        date.fromisoformat(str(text))
    except ValueError:
        return False                    # 2026-02-30 matches the pattern and is no day at all
    return True


def validate(rule: dict) -> None:
    if not isinstance(rule, dict):
        raise AlertError(f"a rule must be an object, not {rule!r}")
    kind = rule.get("type")
    if kind not in PRICE_TYPES:
        raise AlertError(f"rule {rule.get('id')}: unknown type {kind!r}")
    if rule.get("id") is not None and not (isinstance(rule["id"], str) and rule["id"].strip()):
        # A number id is saved in the state as text and never found again, so the rule rang on
        # every check.
        raise AlertError(f"rule {rule.get('id')!r}: id must be text")
    if kind not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError(f"rule {rule.get('id')} needs a symbol")
    if rule.get("symbol") is not None and not (
            isinstance(rule["symbol"], str) and (rule["symbol"].strip() or kind in NO_QUOTE)):
        # An empty symbol on a time or opening-bell rule is no symbol, as the US pack reads it.
        raise AlertError(f"rule {rule.get('id')}: symbol must be text like \"SBIN\"")
    if kind in ("above", "below", "stop", "target") and not _finite(rule.get("price")):
        raise AlertError(f"rule {rule.get('id')} needs a numeric price")
    if kind in ("above", "below", "stop", "target") and not rule["price"] > 0:
        # A stop of -5 can never ring: the alert would sit there looking armed.
        raise AlertError(f"rule {rule.get('id')}: the price must be above 0")
    if kind == "pct_move" and not _finite(rule.get("pct")):
        raise AlertError(f"rule {rule.get('id')} needs a numeric pct")
    if kind == "pct_move" and not rule["pct"] > 0:
        # 0% or -3% is "any move": it rang on +0.04%. A fall is --direction down.
        raise AlertError(f"rule {rule.get('id')}: pct must be above 0 (use --direction down "
                         f"for a fall)")
    if kind == "pct_move" and rule.get("ref") is not None and \
            not (_finite(rule["ref"]) and rule["ref"] > 0):
        raise AlertError(f"rule {rule.get('id')}: ref must be a price above 0")
    if kind == "pct_move" and rule.get("direction") not in (None, "up", "down"):
        # Anything else was read as "down" and rang on a fall the user never asked about.
        raise AlertError(f"rule {rule.get('id')}: direction must be \"up\" or \"down\"")
    if kind == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError(f"rule {rule.get('id')} needs \"at\" as an IST time like \"15:20\"")
    for key in ("label", "note"):
        if rule.get(key) is not None and not isinstance(rule[key], str):
            raise AlertError(f"rule {rule.get('id')}: {key} must be text")
    if rule.get("date") and not _real_date(rule["date"]):
        raise AlertError(f"rule {rule.get('id')}: date must be a real YYYY-MM-DD date")


def rule_id(rule: dict) -> str:
    """The rule's id, or the one it is known by without one ("SBIN-stop"), everywhere alike."""
    return rule.get("id") or f"{rule.get('symbol', 'market')}-{rule.get('type')}"


def active(rule: dict, today: str) -> bool:
    """A rule with a "date" is watched on that date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp: str | None, today: str) -> bool:
    return bool(stamp) and str(stamp)[:10] == today


def market(at: datetime, offline: bool = False) -> dict:
    """What the clock says, in the shape the rules need."""
    weekend = at.weekday() >= 5
    try:
        # Asked for the year even at a weekend, so calendar_known says what it says: whether
        # this year's holiday list is known, as the US pack's does.
        holiday = clock.holiday_name(at.date(), offline=offline)
        known = True
    except clock.Unknown:
        holiday, known = None, False
    trading = known and not weekend and holiday is None
    is_open = trading and clock.OPEN <= at.time() < clock.CLOSE
    if weekend:
        reason = "weekend"
    elif not known:
        reason = "the holiday list for this year is not known"
    elif not trading:
        reason = f"NSE holiday: {holiday.rstrip(clock.MUHURAT)}"
    elif is_open:
        reason = "regular session"
    elif at.time() < clock.OPEN:
        reason = "before the open"
    else:
        reason = "after the close"
    status = {"date": at.date().isoformat(), "weekday": at.strftime("%A"),
              "calendar_known": known, "trading_day": trading, "open": is_open,
              "reason": reason, "now_ist": at.strftime("%Y-%m-%d %H:%M"),
              "opens": clock.OPEN.strftime("%H:%M") if trading else None,
              # No session, no closing time: "closes 15:30" on a Sunday is a session that is not.
              "closes": clock.CLOSE.strftime("%H:%M") if trading else None,
              "say": clock.status(at, offline=offline)}
    try:
        # Always worked out, as the US pack's is; while open it is the next session's.
        status["next_open"] = f"{clock.next_open(at, offline=offline):%Y-%m-%d %H:%M}"
    except clock.Unknown:
        status["next_open"] = None
    return status


def evaluate(rules: list[dict], quotes: dict, status: dict, state: dict,
             at: datetime) -> tuple[list[dict], dict]:
    """Which rules ring now, and the state after. Each rule rings once, as its type says."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    stamp = at.replace(microsecond=0).isoformat()
    rings = []
    for rule in rules:
        validate(rule)
        rid = rule_id(rule)
        if not active(rule, today):
            continue
        kind = {"stop": "below", "target": "above"}.get(rule["type"], rule["type"])

        if kind == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "",
                              "message": f"Opening bell: the NSE is open ({status['now_ist']} IST)."})
                rung[rid] = stamp
            continue

        if kind == "time":
            # A trading day only. Without this a "square off at 15:20" alert rang on a Sunday.
            when = _hhmm(rule.get("at"))
            if status["trading_day"] and at.time() >= when and not _rang_today(rung.get(rid), today):
                label = rule.get("label") or "time alert"
                message = (f"{label[:1].upper()}{label[1:]}: it is {at:%H:%M} IST (set for "
                           f"{when:%H:%M})")
                if status["open"]:
                    message += f", the session closes at {status['closes']}."
                elif at.time() < clock.OPEN:
                    message += f", the session has not opened yet (it opens at {status['opens']})."
                else:
                    message += ", the session has closed."
                symbol = str(rule.get("symbol") or "").strip().upper()
                if symbol:
                    message += (f" If {symbol} is still open, close it: /nse-broker flatten "
                                f"--symbol {symbol} builds the sell ticket, then /nse-risk and "
                                f"your yes.")
                rings.append({"id": rid, "symbol": symbol, "message": message})
                rung[rid] = stamp
            continue

        symbol = rule["symbol"].strip().upper()
        got = quotes.get(symbol)
        if not got:
            continue
        price = got["price"]
        if kind in ("above", "below"):
            hit = price >= rule["price"] if kind == "above" else price <= rule["price"]
            if hit and rid not in rung:
                word = "at or above" if kind == "above" else "at or below"
                what = {"stop": "stop", "target": "target"}.get(rule["type"], "alert level")
                said = rule.get("note") or rule.get("label")
                note = f" ({said})" if said else ""
                rings.append({"id": rid, "symbol": symbol,
                              "message": (f"{symbol} {'is' if status['open'] else 'last'} Rs "
                                          f"{price:,.2f}{_when(got)}: {word} your {what} Rs "
                                          f"{rule['price']:,.2f}{note}.")})
                rung[rid] = stamp
            elif not hit and rid in rung and rule.get("rearm"):
                # Re-armed by the price moving BACK across the level, so the next crossing rings.
                # The first version skipped the once-only guard instead, and a re-armed rule rang
                # on every single check for as long as the price stayed past the level.
                del rung[rid]
        elif kind == "pct_move":
            reference = rule.get("ref") or got.get("previous_close") or got.get("open")
            if not reference:
                continue
            moved = (price / float(reference) - 1) * 100
            want = rule.get("direction")
            hit = abs(moved) >= rule["pct"] and (not want or (want == "up") == (moved > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": symbol,
                              "message": (f"{symbol} moved {moved:+.2f}% to Rs {price:,.2f}"
                                          f"{_when(got)} (from Rs {float(reference):,.2f}).")})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def stale_quotes(quotes: dict, today: str) -> tuple[dict, list[str]]:
    """Split off quotes that are not from today. A price rule never rings on one.

    The quote falls back to the exchange's closing file when the intraday feed is down, and that
    file is a day old. Ringing a stop on it during the session would say SBIN "is" at a price it
    was at yesterday. The US pack has no fallback, so it never meets this; this pack does, so the
    quote's own date decides, and a stale one is reported rather than used.
    """
    fresh, notes = {}, []
    for symbol, q in quotes.items():
        when = str(q.get("at") or "")
        if not when:
            # No time on it, so no way to show it is today's: unverified, not rung on.
            notes.append(f"{symbol}: the quote carries no time, so it cannot be shown to be "
                         f"today's, and no price rule was checked on it")
            continue
        if when[:10] != today:
            notes.append(f"{symbol}: the only price is from {when[:16].replace('T', ' ')} "
                         f"({q.get('source') or 'an older quote'}), not today, so no price rule "
                         f"was checked on it")
            continue
        fresh[symbol] = q
    return fresh, notes


def _when(q: dict) -> str:
    when = str(q.get("at") or "")
    return f" (quote {when[11:16]} IST)" if len(when) >= 16 else ""


def rang(rule: dict, price, previous, at: datetime) -> str | None:
    """Whether this one rule rings now on a fresh state, and what it says. For quick checks."""
    status = market(at, offline=True)
    quotes = {}
    if rule.get("symbol") and price is not None:
        quotes[str(rule["symbol"]).strip().upper()] = {"price": price,
                                                       "previous_close": previous}
    full = dict(rule)
    full.setdefault("id", "probe")
    rings, _ = evaluate([full], quotes, status, {}, at)
    return rings[0]["message"] if rings else None


def rules_from_plan(plan: dict, flat_default: str | None = None) -> list[dict]:
    """The plan as three alerts: the stop, the target and flat-by, for the plan's own day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run /nse-exit first)")
    if not isinstance(plan["symbol"], str) or not plan["symbol"].strip():
        raise AlertError(f"the plan's symbol {plan['symbol']!r} is not a symbol like SBIN")
    symbol = plan["symbol"].strip().upper()
    side = str(plan.get("side") or "buy").strip().lower()
    if side not in ("buy", "long"):
        # The stop and target alerts are a long's: a short's stop sits ABOVE the price, so a
        # "price at or below the stop" alert would ring at once on a trade going its way. This
        # pack refuses short entries, and so refuses alerts for one.
        raise AlertError(f"the plan is a {side!r} trade; plan alerts are for a long (buy) only")
    out = []
    for key, kind, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        value = plan.get(key)
        if not _finite(value) or value <= 0:
            raise AlertError(f"the plan has no {key} (run /nse-exit)")
        label = plan.get(f"{key}_label") or plan.get(f"{key}_why")
        out.append({"id": f"{symbol.lower()}-plan-{key}", "symbol": symbol, "type": kind,
                    "price": float(value), "note": word + (f": {label}" if label else "")})
    if not out[0]["price"] < out[1]["price"]:
        raise AlertError(f"the plan's stop Rs {out[0]['price']:,.2f} is not below its target "
                         f"Rs {out[1]['price']:,.2f}, so it is not a long's plan (run /nse-exit)")
    flat = plan.get("flat_by") or flat_default or FLAT_BY
    if _hhmm(flat) is None:
        raise AlertError(f"the plan's flat_by {flat!r} is not a time like 15:20")
    out.append({"id": f"{symbol.lower()}-flat-by", "symbol": symbol, "type": "time", "at": flat,
                "label": "flat by close"})
    if plan.get("date"):
        if not _real_date(plan["date"]):
            raise AlertError(f"the plan's date {plan['date']!r} is not a YYYY-MM-DD date")
        for rule in out:
            rule["date"] = str(plan["date"])
    for rule in out:
        validate(rule)
    return out


def holdings(portfolio: Path, plan: Path) -> list[str]:
    """Every symbol worth quoting: anything held, and the open plan."""
    names = []
    held = _read(portfolio, {})
    rows = held.get("positions") if isinstance(held, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            names.append(str(row.get("symbol") or row.get("ticker")
                             or row.get("tradingsymbol") or "").strip().upper())
    saved = _read(plan, {})
    if isinstance(saved, dict) and saved.get("symbol"):
        names.append(str(saved["symbol"]).strip().upper())
    return [n for n in dict.fromkeys(names) if n]


# ---- commands ----------------------------------------------------------------------------------

def cmd_clock(a) -> int:
    at = clock.now(a.now)
    status = market(at, offline=bool(a.offline))
    if a.json:
        print(json.dumps(status, indent=2))
    else:
        print(_said(status))
        if status["opens"]:
            print(f"Today's session: {status['opens']} to {status['closes']} IST, with a pre-open "
                  f"auction from 09:00.")
        if not status["open"]:
            print(f"Next open: {status['next_open']} IST." if status["next_open"] else
                  "Next open: not known (the holiday list does not reach that far).")
        if not status["calendar_known"] and "not known" not in status["say"]:
            # A weekend in a year with no list: closed is right, but the list is still unknown.
            print(f"The {status['date'][:4]} NSE holiday list is not known here: check "
                  f"nseindia.com's holiday list for this date.")
    return 0 if status["open"] else 1


def _said(status: dict) -> str:
    """The clock's line, always with the IST time and the day, as the US pack's always has."""
    return f"IST {status['now_ist']} ({status['weekday']}): {status['say']}"


def cmd_quote(a) -> int:
    names = [s.strip().upper() for s in a.symbols if s.strip()]
    if a.holdings:
        names += holdings(a.portfolio, a.plan)
    names = list(dict.fromkeys(names))
    if not names:
        raise AlertError("which symbols? alerts.py quote SBIN ITC, or --holdings")
    at = clock.now(a.now)
    got, errors = quotes_for(names, a.source, a.offline)
    wrote = False
    if a.write and got:
        try:
            write_prices(Path(a.write), got, at, a.source)
            wrote = True
        except OSError as err:
            # Said beside the quotes, rather than stopping before any of them was shown.
            errors.append(f"the prices were not saved to {a.write}: {err}")
    if a.json:
        print(json.dumps({"quotes": got, "errors": errors}, indent=2, default=str))
        return 0 if got else 1
    for symbol in names:
        q = got.get(symbol)
        if q:
            change = ""
            if q.get("previous_close"):
                change = (f" ({(q['price'] / q['previous_close'] - 1) * 100:+.2f}% vs previous "
                          f"close Rs {q['previous_close']:,.2f})")
            print(f"{symbol:<10} Rs {q['price']:,.2f}{change}  {q.get('at') or ''}  "
                  f"[{q.get('source')}]")
    for err in errors:
        print(f"error: {err}")
    if wrote:
        print(f"Wrote {len(got)} price(s) to {a.write}.")
    print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the "
          "one that fills.")
    return 0 if got else 1


def cmd_check(a) -> int:
    doc = _rules(a.rules)
    if not doc["rules"]:
        raise AlertError(f"no rules in {a.rules}: add some with alerts.py add --from-plan")
    for rule in doc["rules"]:
        validate(rule)
    ids = [rule_id(r) for r in doc["rules"]]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        # Two rules under one id share one "rang" mark, so once the first rang the second could
        # never ring: an alert that looks armed and is not.
        raise AlertError(f"{a.rules}: more than one rule has the id {', '.join(twice)}; give "
                         f"each its own \"id\"")
    state = _state(a.state)
    at = clock.now(a.now)
    status = market(at, offline=bool(a.offline))
    watched = [r for r in doc["rules"] if active(r, status["date"])]
    names = list(dict.fromkeys(str(r.get("symbol", "")).strip().upper() for r in watched
                               if r.get("type") not in NO_QUOTE and r.get("symbol")))
    got, errors = quotes_for(names, a.source, a.offline) if names else ({}, [])
    usable, stale = stale_quotes(got, status["date"])
    errors += stale
    rings, state = evaluate(doc["rules"], usable, status, state, at)
    if not a.no_write:
        _write(a.state, state)
        if got:
            try:
                write_prices(a.prices_out, got, at, a.source)
            except OSError as err:
                # Said, and the check goes on. Stopping here, after the state already marked a
                # rule as rung, lost that alert for good: never shown, and never to ring again.
                errors.append(f"the prices were not saved to {a.prices_out}: {err}")
    if a.json:
        # Only the prices a rule was checked on: a set-aside one is in "errors", with its date.
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in usable.items()}}, indent=2))
        return 0 if rings else 1
    if not a.quiet:
        print(_said(status))
    for ring in rings:
        print(f"ALERT {ring['message']}")
    if not rings and not a.quiet:
        # Only today's prices: listing a set-aside one here read as the price now.
        prices = ", ".join(f"{s} Rs {usable[s]['price']:,.2f}" for s in names if s in usable)
        print(f"Nothing hit. {len(watched)} rule(s) watched today"
              + (f", {prices}" if prices else "") + ".")
    for err in errors:
        print(f"error: {err}")
    if rings and not a.quiet:
        print("An alert is information, not an order; this skill sold and bought nothing. An "
              "intraday trade's SL-M stop already sits at Zerodha: check get_positions and record "
              "any fill (/nse-broker fill). To close a position yourself: /nse-broker flatten, "
              "then /nse-risk and your yes.")
    return 0 if rings else 1


def _n(x) -> str:
    """A level as an id, exact, as the US pack writes it. {:g} kept six digits, so a stop at
    140000.50 became 140000 and a second stop at 140000.25 was refused as a duplicate."""
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def cmd_add(a) -> int:
    doc = _rules(a.rules)
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError(f"no plan at {a.plan} (run /nse-exit first)")
        try:
            conf = settings.load(a.config)
        except FileNotFoundError as err:
            raise AlertError(f"{err} (the --config file for flat_by)") from None
        new = rules_from_plan(plan, (conf or {}).get("flat_by"))
        ids = [r["id"] for r in new]       # a list: an id in the file that is an object is unhashable
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _state(a.state)
        if state.get("rung"):
            for rid in ids:
                state["rung"].pop(rid, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for rule in new:
            print(f"Added {rule['id']}: {json.dumps(rule)}")
        print("Run alerts.py check every few minutes while the trade is open (a /loop). Free "
              "quotes are delayed, and a price can pass a level between two checks.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:20")
        rule = {"id": a.id or f"time-{a.at.replace(':', '')}", "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol and a.symbol.strip():
            rule["symbol"] = a.symbol.strip().upper()
    else:
        if not a.symbol or not a.symbol.strip():
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        symbol = a.symbol.strip().upper()
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        value = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or f"{symbol.lower()}-{rule['type']}-{_n(value)}"},
                    symbol=symbol, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
        if a.label:
            rule["label"] = a.label
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(rule_id(r) == rule["id"] for r in doc["rules"]):
        raise AlertError(f"a rule with id {rule['id']} already exists")
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print(f"Added {rule['id']}: {json.dumps(rule)}")
    return 0


def cmd_list(a) -> int:
    doc = _rules(a.rules)
    state = _state(a.state)
    if not doc["rules"]:
        print("No rules yet. Try: alerts.py add --from-plan")
        return 1
    if a.json:
        print(json.dumps({"rules": doc["rules"], "rung": state.get("rung", {})}, indent=2))
        return 0
    for rule in doc["rules"]:
        rid = rule_id(rule)      # a rule with no id crashed the list; it shows the id it rings by
        # Only a text id can have rung (check refuses any other), and {"id": {...}} crashed here.
        when = state.get("rung", {}).get(rid) if isinstance(rid, str) else None
        print(f"{str(rid):<26} {json.dumps({k: v for k, v in rule.items() if k != 'id'})}"
              + (f"  (rang {when})" if when else ""))
    return 0


def cmd_reset(a) -> int:
    state = _state(a.state)
    if a.id not in state.get("rung", {}):
        print(f"{a.id} has not rung (nothing to reset).")
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print(f"Re-armed {a.id}.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    rules_file, state_file = show.shared("alerts.json"), show.shared("state") / "alerts.json"

    def net(p):
        p.add_argument("--source", default="feed", choices=["feed", "exchange"],
                       help="feed: the intraday feed, falling back to the exchange's own file; "
                            "exchange: the exchange's closing file only")
        p.add_argument("--offline", metavar="DIR",
                       help="read saved <SYMBOL>.json (or .json.gz) quotes, no network")
        p.add_argument("--now", help="pin the clock, like '2026-10-22 15:20' (IST; an ISO time "
                                     "ending Z is UTC)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    p.add_argument("--now", help="pin the clock, like '2026-10-22 15:20' (IST; an ISO time "
                                 "ending Z is UTC)")
    p.add_argument("--offline", action="store_true", help="use the built-in holiday list")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add the portfolio and the plan")
    p.add_argument("--portfolio", type=Path, default=show.shared("portfolio.json"))
    p.add_argument("--plan", type=Path, default=show.shared("plan.json"))
    p.add_argument("--write", nargs="?", const=str(show.shared("prices.json")), metavar="PATH",
                   help="merge into prices.json (default nse/prices.json)")

    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", type=Path, default=rules_file)
    p.add_argument("--state", type=Path, default=state_file)
    p.add_argument("--prices-out", type=Path, default=show.shared("prices.json"))
    p.add_argument("--no-write", action="store_true", help="look without recording anything")
    p.add_argument("--quiet", action="store_true", help="say only what rang")

    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", type=Path, default=rules_file)
    p.add_argument("--state", type=Path, default=state_file)
    p.add_argument("--symbol")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--above", type=show.number)
    group.add_argument("--below", type=show.number)
    group.add_argument("--stop", type=show.number)
    group.add_argument("--target", type=show.number)
    group.add_argument("--pct", type=show.number)
    group.add_argument("--opening-bell", action="store_true")
    group.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    group.add_argument("--from-plan", action="store_true",
                       help="the stop, the target and flat-by from the plan")
    p.add_argument("--plan", type=Path, default=show.shared("plan.json"))
    p.add_argument("--config", type=Path, help="flat_by when the plan has none")
    p.add_argument("--label", help="what the alert says, e.g. \"square off\"")
    p.add_argument("--date", help="watch this rule on this date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true",
                   help="ring again after the price has moved back across the level")
    p.add_argument("--note")
    p.add_argument("--id")

    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", type=Path, default=rules_file)
    p.add_argument("--state", type=Path, default=state_file)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", type=Path, default=state_file)

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as err:
        print(f"alerts.py: {err}", file=sys.stderr)
        return 1
    except OSError as err:
        # A path that is a folder, or a file that cannot be written: said, not a traceback.
        print(f"alerts.py: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    show.run(main)
