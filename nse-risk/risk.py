#!/usr/bin/env python3
"""The gate. Checks every proposed order against your rules and answers PASS or BLOCK for each.

Reads the orders (nse/orders.json, from /nse-budget) and checks each one against your rules
(config.json), your account (nse/portfolio.json), fresh prices (nse/prices.json) and what has
already gone out today. Orders are checked in file order as if each PASS had filled, so a batch
that is fine order by order but breaks a limit together is caught.

    python risk.py                          # nse/orders.json; records nse/state/risk.json
    python risk.py --now "2026-10-22 10:20" # pin the clock (IST)
    python risk.py --orders mine.json --json
    python risk.py --no-write               # check without recording the verdicts
    python risk.py --entry 961.85 --stop 950 # or size one idea from scratch

Exit 0 when every order passes, 1 when any is blocked. Each verdict is recorded with a fingerprint
of the exact order, so a later step can tell that THIS order passed today and not a changed one.

Needs no network and no account: the holiday list is the built-in one unless you pass --online,
which asks the exchange for it. This tool never places an order.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import show                                      # noqa: E402

DEFAULTS = {"capital": 100000.0, "risk_per_trade_percent": 1.0, "max_positions": 5,
            "max_trades_per_day": 3, "max_daily_loss_percent": 3.0, "min_stop_percent": 1.0}

# India-specific costs, as of 2026. Rounded up rather than down: a sizing tool that flatters the
# arithmetic is worse than none. These are for a delivery trade on NSE equity; intraday rates differ
# and a discount broker's brokerage may be lower, which only makes the real figure better than this.
COSTS = {
    "brokerage_percent": 0.03,        # a flat-fee broker is cheaper; this is a safe upper bound
    "stt_percent": 0.1,               # securities transaction tax, delivery, on both sides
    "exchange_percent": 0.00325,      # NSE transaction charge
    "gst_percent": 18.0,              # on brokerage, exchange charges and the SEBI fee
    "sebi_percent": 0.0001,
    "stamp_percent": 0.015,           # on buy only
}

# A stop closer than this to the entry is usually noise rather than a level: on an Indian mid cap a
# 0.5% band is inside one minute's range, so the position it implies is enormous and the stop will
# be taken out by nothing at all.
MIN_STOP_PERCENT = 1.0
# And a stop further than this means the idea is a different size of bet than this tool assumes.
MAX_STOP_PERCENT = 25.0


def settings(path: Path | None) -> dict:
    """The limits for sizing one idea, from --config or the usual place.

    Read as the gate reads them: "500000" is 500000, and text that is not a number, or a negative
    figure, is refused (GateError). Ignoring it and keeping the default would size against a
    capital that is not yours with nothing on screen to say so. A config with `budget` and no
    `capital` sizes against the budget. `_capital_default` says when the capital is the default.
    """
    from nsetools import settings as rules_file
    out = dict(DEFAULTS)
    raw = dict(rules_file.load(path))
    if raw.get("capital") is None and raw.get("budget") is not None:
        raw["capital"] = raw["budget"]
    for key in DEFAULTS:
        value = _limit(raw, key)
        if value is not None:
            out[key] = value
    out["_capital_default"] = _limit(raw, "capital") is None
    if raw.get("_from"):
        out["_from"] = raw["_from"]
    return out


def costs(value: float, buying: bool) -> dict:
    """What the trade costs before it has done anything, in rupees."""
    brokerage = value * COSTS["brokerage_percent"] / 100
    stt = value * COSTS["stt_percent"] / 100
    exchange = value * COSTS["exchange_percent"] / 100
    sebi = value * COSTS["sebi_percent"] / 100
    stamp = value * COSTS["stamp_percent"] / 100 if buying else 0.0
    # GST falls on the brokerage, the exchange charge and the SEBI fee alike.
    gst = (brokerage + exchange + sebi) * COSTS["gst_percent"] / 100
    total = brokerage + stt + exchange + sebi + stamp + gst
    return {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi,
            "stamp": stamp, "gst": gst, "total": total}


def size(entry: float, stop: float, conf: dict, target: float | None = None,
         open_positions: int = 0) -> dict:
    """How many shares, or why none. Every refusal says what would have to change."""
    capital = conf["capital"]
    risk_percent = conf["risk_per_trade_percent"]
    blocks: list[str] = []
    notes: list[str] = []

    if entry <= 0 or stop <= 0:
        blocks.append("entry and stop must both be positive")
    if stop >= entry:
        blocks.append(f"stop {stop:,.2f} is at or above entry {entry:,.2f}; "
                      f"this tool sizes long trades only")

    floor = float(conf.get("min_stop_percent", MIN_STOP_PERCENT))
    stop_percent = (entry - stop) / entry * 100 if entry > 0 and stop < entry else 0.0
    # The floor in whole paise, as above: a stop exactly at it is not "closer than" it.
    if 0 < stop_percent and (round((entry - stop) * 100) * 100
                             < round(floor * round(entry * 100), 6)):
        blocks.append(f"stop is {stop_percent:.2f}% away, closer than {floor}%: "
                      f"inside one minute's range on most Indian mid caps, so it would be taken "
                      f"out by noise and the size it implies is unreal")
    if stop_percent > MAX_STOP_PERCENT:
        blocks.append(f"stop is {stop_percent:.1f}% away, beyond {MAX_STOP_PERCENT}%: "
                      f"that is a different size of bet than this tool assumes")

    if open_positions >= conf["max_positions"]:
        blocks.append(f"{open_positions} positions already open, limit is "
                      f"{int(conf['max_positions'])}")

    risk_rupees = capital * risk_percent / 100
    per_share_risk = max(entry - stop, 0.0)
    shares = int(risk_rupees / per_share_risk) if per_share_risk > 0 else 0

    value = shares * entry
    if value > capital:
        shares = int(capital / entry) if entry > 0 else 0
        value = shares * entry
        notes.append("size cut to fit capital; the stop allowed more than you have")

    if shares <= 0 and not blocks:
        blocks.append(f"one share costs {entry:,.2f} and the risk budget is "
                      f"{risk_rupees:,.2f}; the smallest position already risks more than allowed")

    buy = costs(value, buying=True)
    sell_at_stop = costs(shares * stop, buying=False)
    round_trip = buy["total"] + sell_at_stop["total"]
    loss_at_stop = shares * per_share_risk + round_trip

    # Costs are not a footnote. On a tight stop they are a material share of what you are risking.
    #
    # The threshold is arithmetic rather than taste. The round trip is about 0.293% of the position
    # (0.154% buying, 0.139% selling), and the risk budget is shares times the stop distance, so
    # costs against budget comes to roughly 0.00293 x entry / distance -- it depends only on how
    # tight the stop is, not on the capital or the size.
    #
    #   stop 1% away  -> 29% of the budget      stop 3% away  -> 10%
    #   stop 2% away  -> 15%                    stop 5% away  ->  6%
    #
    # This was first written as a third of the budget, which a test showed could never happen: a
    # third needs a stop inside 0.89%, and anything under 1% is blocked above. The check was dead.
    # 15% fires for a stop tighter than about 2%, which is the band worth a word.
    if shares > 0 and round_trip > risk_rupees * 0.15:
        notes.append(f"costs are {round_trip/risk_rupees*100:.0f}% of the risk budget, because the "
                     f"stop is {stop_percent:.1f}% away; a wider stop carries them better")

    out = {
        "verdict": "BLOCK" if blocks else "PASS",
        "shares": shares if not blocks else 0,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "stop_percent": round(stop_percent, 2),
        "position_value": round(value, 2) if not blocks else 0.0,
        "capital": round(capital, 2),
        "position_percent_of_capital": round(value / capital * 100, 1) if capital and not blocks else 0.0,
        "risk_budget": round(risk_rupees, 2),
        "risk_per_share": round(per_share_risk, 2),
        "costs_round_trip": round(round_trip, 2) if not blocks else 0.0,
        "loss_if_stopped": round(loss_at_stop, 2) if not blocks else 0.0,
        "reasons": blocks,
        "notes": notes,
    }

    if target and target > entry and not blocks:
        gain = shares * (target - entry) - buy["total"] - costs(shares * target, False)["total"]
        out["target"] = round(target, 2)
        out["gain_if_target"] = round(gain, 2)
        out["reward_to_risk"] = round(gain / loss_at_stop, 2) if loss_at_stop > 0 else None
        if out["reward_to_risk"] is not None and out["reward_to_risk"] < 1.5:
            out["notes"].append(f"reward to risk is {out['reward_to_risk']}, under 1.5 after costs; "
                                f"you need to be right more often than not for this to pay")
    return out




# ---- the gate: checking orders someone else sized ------------------------------------------------
#
# Modelled check for check on the US pack's trader-risk. What blocks an order:
#
#   every order    format, tick, asset, short
#   an entry       price, blocked, size, position, cash, duplicate, daily limit, stop, loss,
#                  reward:risk, daily loss, trades, window -- plus Indian ones: the MTF product,
#                  the number of positions open (max_positions, which config.example.json
#                  promises), and the noise floor and ceiling on how far away a stop can sit
#
# An ENTRY opens or adds to a position: a buy, or a sell that would go short. An EXIT only reduces
# what you hold. Getting out must always be possible, so an exit is checked for format, asset and
# short sales only, and every other rule becomes a warning for it.
#
# Orders are checked IN FILE ORDER, as if each one that passed had filled, so a batch that is fine
# order by order but breaks a limit together is caught.
#
# Left out on purpose, because it does not exist here: the pattern-day-trader rule is a US FINRA
# rule, there are no early closes on the NSE, and there are no notional (rupee-amount) or
# fractional equity orders, so the US checks for those have nothing to check.

# No default capital. With no capital in the config and no portfolio to read the cash from, the
# gate refuses, as the US pack refuses with no budget and no portfolio: checking against an assumed
# 1 lakh would pass or block on an account that is not yours.
GATE_DEFAULTS = {
    "capital": None, "cash_buffer_pct": 10.0, "max_position_pct": 75.0,
    "max_order_rupees": 40000.0, "min_order_rupees": 500.0,
    "risk_per_trade_percent": 1.0, "max_loss_per_trade_rupees": 0,
    "max_daily_loss_percent": 3.0, "max_daily_loss_rupees": 0,
    "max_trades_per_day": 3, "max_orders_per_day": 10, "max_positions": 5,
    "min_reward_risk": 1.0, "require_stop": True, "min_stop_percent": MIN_STOP_PERCENT,
    "no_entries_before": "09:30", "no_entries_after": "15:00",
    "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False,
}
SIDES = ("buy", "sell")
ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
VALIDITIES = {"DAY", "IOC", "TTL"}
PRODUCTS = {"MIS", "CNC", "NRML", "MTF"}
EXCHANGES = {"NSE", "BSE", "NFO", "BFO"}       # NFO and BFO only with "allow_options"
FRESH_MINUTES = 15
PAISA = 0.005

# An NSE equity symbol: capitals, digits and the few characters the exchange uses (M&M, BAJAJ-AUTO).
import re as _re                                                   # noqa: E402

SYMBOL = _re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
# A derivative contract ends in a strike and CE/PE, or in FUT. That covers the monthly form
# (NIFTY26OCT25000CE, RELIANCE26DECFUT) and the weekly one, whose expiry is year, one-character
# month and day (NIFTY2610625000CE, NIFTY26O2725000CE) and which an expiry-shaped pattern missed.
# Checked against the 2,713 symbols in the real equity list and bhavcopy fixtures: none matches.
DERIVATIVE = _re.compile(r"(\d(\.\d+)?(CE|PE)|FUT)$")
# Not an equity at all: a crypto or currency pair (BTCUSD, USDINR, EURUSDT). No NSE equity symbol
# in those same 2,713 ends this way.
NOT_EQUITY = _re.compile(r"(USD|USDT|USDC|INR|EUR|GBP|JPY)$")
DERIVATIVE_EXCHANGES = {"NFO", "BFO"}


class GateError(Exception):
    """The gate cannot run at all: a missing or unreadable file. Never a quiet default."""


def _num(value):
    try:
        number = None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None
    # JSON "NaN" loads as a float; a nan stop would slip past every "<" check, so it is no number.
    return number if number is None or math.isfinite(number) else None


def _rupees(value) -> str:
    return f"Rs {float(value):,.2f}"


def _with_costs(loss: float, entry_value: float, stop_value: float, long: bool, product: str,
                exchange: str) -> str:
    """", Rs X with ... charges" for the info line, or nothing where the figure would not be true.

    In at the entry, out at the stop: a long buys first, a short buys back at the stop, and stamp
    duty falls on whichever side is the buy. The rates are this module's NSE delivery ones, each
    rounded up, so for an NSE intraday (MIS) trade the figure is a ceiling. A delivery sale also
    pays a fixed depository charge (and MTF pays interest), which no rate here covers, so for CNC
    and MTF the figure is said to be before it. BSE's charges and the derivatives' are other rates
    altogether: no figure rather than a wrong one.
    """
    if exchange != "NSE" or product not in ("MIS", "CNC", "MTF"):
        return ""
    trip = costs(entry_value, long)["total"] + costs(stop_value, not long)["total"]
    parts = "brokerage, STT, exchange and SEBI charges, GST and stamp duty"
    if product == "MIS":
        return f", at most {_rupees(loss + trip)} with {parts}"
    extra = "the depository charge on the sale" + (" and the MTF interest" if product == "MTF"
                                                   else "")
    return f", {_rupees(loss + trip)} with {parts}, before {extra}"


def _count(value: float) -> str:
    """A share count as a person writes it: 41, 1,000,000 or 2.5, never 1e+06."""
    value = float(value)
    return f"{int(value):,}" if value == int(value) else f"{value:,.4f}".rstrip("0")


def _paisa(value: float) -> float:
    """A price to the paisa, half up (961.855 -> 961.86), as the US pack's _cent."""
    return int(float(value) * 100 + 0.5 + 1e-9) / 100.0


def _limit(conf: dict, key: str, default=None):
    """A limit from the config as a float, or `default` when it is missing or empty.

    "10" is read as 10, as the US pack's float() reads it. Text that is not a number is refused
    rather than guessed at: a limit read wrongly compares wrongly.
    """
    raw = conf.get(key)
    if raw is None or raw == "" or raw is False:
        return default
    value = _num(raw)
    if value is None:
        raise GateError(f"{key} in the config must be a number, got {raw!r}")
    if value < 0:
        # A negative limit is no limit anyone meant: refused, not compared.
        raise GateError(f"{key} in the config cannot be negative, got {raw!r}")
    return value


def _is_entry_record(rec: dict) -> bool:
    """/nse-broker logs the kind this gate gave; an older record without one: a buy is an entry."""
    if rec.get("kind"):
        return rec["kind"] == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def _off_tick(value: float, basis: float) -> bool:
    """True when a price is not a whole number of ticks for a stock trading near `basis`."""
    from nsetools import clock
    tick = clock.tick_size(basis)
    steps = round(value / tick, 6)
    return abs(steps - round(steps)) > 1e-6


# How /nse-broker places the exits, word for word the same wherever it is said (SKILL.md too).
HOW_EXITS_ARE_PLACED = ("MIS entry, then an SL-M at the stop after each fill; the target is never "
                        "placed at Zerodha for MIS (watched by /nse-alerts); CNC/MTF get a GTT "
                        "(two-leg, or single-leg when stop only)")


def _exits_note(order: dict, product: str, side: str) -> str:
    """Where the stop and target of an order live, said the way /nse-broker places them.

    Verified against Zerodha's own order tool: its `variety` takes regular, co, amo, iceberg or
    auction, and has no bracket. The exits travel with the order only as a plan until /nse-broker
    places them (ticket.py protection_call): for MIS the entry, then an SL-M at the stop after each
    fill, the target never placed at Zerodha but watched by /nse-alerts; for CNC and MTF one GTT,
    two-leg with a stop and a target, single-leg with a stop only.
    """
    stop = order.get("stop_loss_price") is not None
    target = order.get("take_profit_price") is not None
    said = "the stop and target are" if stop and target else ("the stop is" if stop else
                                                               "the target is")
    note = (f"{said} not at the broker yet: Zerodha has no bracket order, so /nse-broker places "
            f"the exits itself: {HOW_EXITS_ARE_PLACED}")
    # Where this order is not the usual case, said too: the rule above would otherwise promise a
    # protection /nse-broker does not place for it (ticket.py protection_call returns none).
    if side == "sell" and product != "MIS":
        return (f"{note}. This {product} sell gets none: /nse-broker protects a {product} "
                f"position only once it is bought")
    if not stop:
        what = "a GTT needs a stop" if product in ("CNC", "MTF") else "an SL-M needs a stop"
        return f"{note}. This order has only a target, so nothing is placed for it: {what}"
    if product not in ("MIS", "CNC", "MTF"):
        return (f"{note}. This {product} order is protected as MIS is: an SL-M at the stop after "
                f"each fill, and its target is not placed at Zerodha either")
    return note


def fingerprint(order: dict) -> str:
    """The exact order that was checked, so a later step can tell it is the same one.

    Built from the fields a broker would act on, never from the notes. The function itself lives
    in nsetools.orders, so /nse-broker recognises an order by exactly the same hash.
    """
    from nsetools import orders
    return orders.fingerprint(order)


def load_orders(path: Path) -> list[dict]:
    """The orders file, in any of the shapes it has had: a header with a list, a list, one order."""
    if not path.exists():
        raise GateError(f"no orders at {path}: run /nse-budget first, or pass --entry and --stop")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise GateError(f"{path} is not valid JSON: {err}") from err
    except OSError as err:
        raise GateError(f"{path} cannot be read: {err}") from err
    if isinstance(doc, dict) and isinstance(doc.get("orders"), list):
        orders = doc["orders"]
    elif isinstance(doc, list):
        orders = doc
    elif isinstance(doc, dict) and doc.get("symbol"):
        orders = [doc]
    else:
        orders = []
    if not orders:
        raise GateError(f"no orders in {path}")
    for number, order in enumerate(orders, 1):
        if not isinstance(order, dict):
            raise GateError(f"order {number} in {path} is not an order (a JSON object), "
                            f"got {order!r}")
    return orders


def load_prices(path: Path | None) -> tuple[dict[str, float], dict[str, str | None]]:
    """{symbol: price} and {symbol: that price's own quote time}. A price with no time is never fresh.

    The time is the quote's own (detail.at), never the file's as_of: /nse-alerts merges quotes into
    one file, so quoting ITC now rewrites as_of while the SBIN price beside it may be yesterday's,
    and judging SBIN by as_of would pass a stale price as fresh. /nse-budget reads it the same way.
    A damaged file is refused, as the US pack refuses it: read as empty, it would quietly measure
    the risk from the plan alone when the user had refreshed the price to stop exactly that.
    """
    if not path or not path.exists():
        return {}, {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise GateError(f"{path} is not valid JSON: {err}") from err
    except OSError as err:
        raise GateError(f"{path} cannot be read: {err}") from err
    # The usual shape {"prices": {...}, "detail": {...}}, or a plain {SYMBOL: price}, as the US
    # pack reads either. Any other shape is a damaged file, refused like one.
    prices = doc.get("prices", doc) if isinstance(doc, dict) else None
    if not isinstance(prices, dict):
        raise GateError(f"{path} is not a prices file: it needs {{\"prices\": {{SYMBOL: price}}}}")
    detail = doc.get("detail")
    detail = {str(k).strip().upper(): v for k, v in detail.items()} if isinstance(detail, dict) else {}
    out, times = {}, {}
    for symbol, price in prices.items():
        if _num(price) is None or _num(price) <= 0:
            continue
        name = str(symbol).strip().upper()
        out[name] = float(price)
        own = detail.get(name)
        times[name] = own.get("at") if isinstance(own, dict) else None
    return out, times


def account(conf: dict, portfolio: dict | None, prices: dict[str, float]) -> tuple[dict, list[str]]:
    """Cash and what is held. With no portfolio file, a fresh account funded with the capital;
    with neither, nothing to check the orders against, so a refusal."""
    warnings: list[str] = []
    if portfolio is None:
        if not conf.get("capital"):
            raise GateError("no capital in the config and no nse/portfolio.json: nothing to check "
                            "the orders against. Set \"capital\" in config.json (copy "
                            "config.example.json), or record your account in nse/portfolio.json")
        return {"cash": float(conf["capital"]), "shares": {}, "values": {}}, warnings
    if not isinstance(portfolio, dict):
        raise GateError("portfolio.json must be a JSON object with \"cash\" and \"positions\"")
    cash = _num(portfolio.get("cash"))
    if cash is None:
        raise GateError("portfolio.json needs a \"cash\" figure in rupees")
    rows = portfolio.get("positions") or []
    if not isinstance(rows, list):
        raise GateError("portfolio.json \"positions\" must be a list")
    shares: dict[str, float] = {}
    values: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise GateError(f"portfolio.json has a position that is not an object: {row!r}")
        # /nse-broker writes "symbol"; "ticker" and Zerodha's "tradingsymbol" are read too.
        symbol = str(row.get("symbol") or row.get("ticker") or row.get("tradingsymbol")
                     or "").strip().upper()
        if not symbol:
            continue
        lots = [lot for lot in (row.get("lots") or []) if isinstance(lot, dict)] \
            if isinstance(row.get("lots"), list) else []
        raw = next((row[k] for k in ("shares", "quantity", "qty") if row.get(k) is not None), None)
        if raw is None and lots:
            parts = [_num(lot.get("shares")) for lot in lots]
            raw = None if None in parts else sum(parts)
        held = _num(raw)
        if held is None:
            # Read as nought, a sale of what you hold would be blocked as a short "(0 shares)":
            # a false figure. Said instead.
            raise GateError(f"portfolio.json: the share count for {symbol} cannot be read "
                            f"({raw!r})")
        shares[symbol] = shares.get(symbol, 0.0) + held
        if prices.get(symbol):
            values[symbol] = shares[symbol] * prices[symbol]
        else:
            cost = _num(row.get("cost"))
            if cost is None:
                cost = sum(_num(lot.get("cost")) or 0.0 for lot in lots)
            values[symbol] = values.get(symbol, 0.0) + cost
            warnings.append(f"no price for {symbol}: valued at its cost of {_rupees(cost)}")
    return {"cash": cash, "shares": shares, "values": values}, warnings


def _window(at, conf: dict, offline: bool) -> list[str]:
    """Why an entry cannot be placed now, IST. Empty when the window is open or switched off."""
    from datetime import time as clock_time
    from nsetools import clock

    def hhmm(raw, name):
        if raw in (None, "", False):
            return None
        # HH:MM as the US pack reads it ("9:30" is fine, "9:5" and "09:30:00" are not).
        found = _re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(raw).strip()) \
            if isinstance(raw, str) else None
        if not found:
            raise GateError(f"{name} must be an IST time like \"09:30\", got {raw!r}")
        return clock_time(int(found.group(1)), int(found.group(2)))

    before = hhmm(conf.get("no_entries_before"), "no_entries_before")
    after = hhmm(conf.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return []
    stamp = at.strftime("%a %Y-%m-%d %H:%M")
    if at.weekday() >= 5:
        # A weekend needs no holiday list to be called closed.
        return [f"window: the market is closed today (a weekend, {stamp} IST)"]
    try:
        trading = clock.is_trading_day(at.date(), offline=offline)
    except clock.Unknown as err:
        if offline:
            return [f"window: the {at.year} NSE holiday list is not built in here, so whether "
                    f"{at:%Y-%m-%d} is a trading day cannot be said; run with --online to fetch "
                    f"it from the exchange"]
        return [f"window: {err}"]
    if not trading:
        name = clock.holiday_name(at.date(), offline=offline)
        why = name.rstrip("*") if name else "a weekend"
        return [f"window: the market is closed today ({why}, {stamp} IST)"]
    now = at.time().replace(second=0, microsecond=0)
    if now < clock.OPEN or at.time() >= clock.CLOSE:
        return [f"window: the market is closed ({stamp} IST; the session is "
                f"{clock.OPEN:%H:%M} to {clock.CLOSE:%H:%M})"]
    if before is not None and now < before:
        return [f"window: no new entries before {before:%H:%M} IST (it is {now:%H:%M})"]
    if after is not None and now > after:
        return [f"window: no new entries after {after:%H:%M} IST (it is {now:%H:%M})"]
    return []


def placed_today(broker_state: dict | None, risk_state: dict | None, today: str) -> list[dict]:
    """What has already gone out today, from the broker's own log where there is one.

    As the US pack does, what /nse-broker recorded as PLACED today. Where there is no broker log
    (the broker skill not used), it counts what THIS gate passed today, one record per distinct
    order fingerprint -- checking the same order twice is not two trades. That is the honest
    substitute: it is what could have been placed, and it errs towards the limit binding.
    """
    if isinstance(broker_state, dict) and isinstance(broker_state.get("orders"), list):
        return [r for r in broker_state["orders"] if isinstance(r, dict)
                and r.get("status") == "placed" and str(r.get("date") or r.get("at", ""))[:10] == today]
    if isinstance(risk_state, dict) and risk_state.get("date") == today \
            and isinstance(risk_state.get("passed"), list):
        seen, out = set(), []
        for rec in risk_state["passed"]:
            if not isinstance(rec, dict):
                continue
            mark = rec.get("fingerprint")
            # A record with no fingerprint cannot be told apart from another, so each one counts:
            # folding them all into one would let the day's limits undercount.
            if mark is None or mark not in seen:
                seen.add(mark)
                out.append(rec)
        return out
    return []


def check(orders: list[dict], conf: dict, portfolio: dict | None = None,
          prices: dict[str, float] | None = None, price_times: dict | None = None,
          broker_state: dict | None = None, risk_state: dict | None = None,
          at=None, offline: bool = True, today: str | None = None) -> dict:
    """Every order PASS or BLOCK with its reasons. It reads no file; with offline=False (the
    --online flag) it asks the exchange for the holiday list, otherwise it uses the built-in one.

    `at` is the clock (the window and how fresh a price is are judged on it); `today` is the day
    the limits count from, which defaults to at's date, as the US pack's --today does."""
    from datetime import datetime, timedelta
    from nsetools import clock

    c = dict(GATE_DEFAULTS)
    c.update({k: v for k, v in (conf or {}).items() if not str(k).startswith("_")})
    if (conf or {}).get("budget") is not None and (conf or {}).get("capital") is None:
        c["capital"] = c["budget"]
    # A switch must be true or false. "false" written as text is truthy, and read that way it
    # would turn options on, or call a live account paper.
    for key in ("allow_options", "allow_margin", "allow_short", "require_stop", "paper"):
        if c.get(key) is not None and not isinstance(c.get(key), bool):
            raise GateError(f"{key} in the config must be true or false, got {c[key]!r}")
        if c.get(key) is None and key == "paper":
            # Live or paper decides whether real money moves: a null there is refused, as
            # /nse-broker refuses it, never guessed either way. Left out, it is paper.
            raise GateError("paper in the config must be true or false, got None")
        if c.get(key) is None:
            # null keeps the safe default for the other switches.
            c[key] = GATE_DEFAULTS[key]
    # "blocked": "SBIN" read as a list is S, B, I, N: SBIN itself would pass.
    if isinstance(c.get("blocked"), str):
        c["blocked"] = [c["blocked"]]
    if c.get("blocked") is not None and not isinstance(c.get("blocked"), list):
        raise GateError(f"blocked in the config must be a list of symbols, got {c['blocked']!r}")
    # Every numeric limit read once, as a float, so a "10" in the config compares and prints as 10.
    capital = _limit(c, "capital", None)
    c["capital"] = capital
    buffer_pct = _limit(c, "cash_buffer_pct", 0.0)
    position_pct = _limit(c, "max_position_pct", 0.0) or 100.0
    least_order = _limit(c, "min_order_rupees", 0.0)
    most_order = _limit(c, "max_order_rupees", 0.0)
    least_rr = _limit(c, "min_reward_risk", 0.0)
    cap_trades = int(_limit(c, "max_trades_per_day", 0.0))
    cap_orders = int(_limit(c, "max_orders_per_day", 0.0))
    cap_positions = int(_limit(c, "max_positions", 0.0))
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items()
              if _num(v) and _num(v) > 0}
    price_times = price_times or {}
    at = at or clock.now()
    today = today or at.date().isoformat()

    acct, top_warn = account(c, portfolio, prices)
    if not capital:
        # The percent limits need a capital to be a percent of. With none in the config, the
        # account itself is the honest figure, and it is said, never used quietly.
        capital = acct["cash"] + sum(acct["values"].values())
        c["capital"] = capital
        top_warn.append(f"no capital in your config: the percent limits (risk per trade, daily "
                        f"loss) are worked from this account's {_rupees(capital)}, its cash plus "
                        f"holdings in portfolio.json")
    blocked ={str(b).upper() for b in c.get("blocked") or []}
    done = placed_today(broker_state, risk_state, today)
    # Re-checking an order is not a second trade -- but only when what counts as "done" is this
    # gate's own passes. Once /nse-broker's log says an order was PLACED, the same order again is
    # a second order, and the US pack calls any symbol and side already placed today a duplicate.
    from_broker = isinstance(broker_state, dict) and isinstance(broker_state.get("orders"), list)
    done_prints = set() if from_broker else {r.get("fingerprint") for r in done}
    entries_today = [r for r in done if _is_entry_record(r)]
    loss_today = 0.0
    for r in entries_today:
        loss = _num(r.get("max_loss"))
        if loss is None:
            top_warn.append(f"an entry today ({r.get('side')} {r.get('symbol')}) has no max loss "
                            f"recorded; it is not counted in the daily loss")
        else:
            loss_today += loss
    placed_keys = {(str(r.get("symbol", "")).upper(), str(r.get("side", "")).lower())
                   for r in done}
    window_reasons = _window(at, c, offline)

    per_trade = _limit(c, "max_loss_per_trade_rupees", 0.0) or \
        capital * _limit(c, "risk_per_trade_percent", 0.0) / 100
    per_day = _limit(c, "max_daily_loss_rupees", 0.0) or \
        capital * _limit(c, "max_daily_loss_percent", 0.0) / 100
    # 0 switches the floor off; only a missing value falls back to the default.
    floor = _limit(c, "min_stop_percent", MIN_STOP_PERCENT)

    if not c.get("paper", True):
        top_warn.append('LIVE account ("paper": false): these orders would use real money')

    def fresh(symbol):
        if symbol not in prices:
            return None
        stamp = price_times.get(symbol)
        if not stamp:
            return None
        try:
            seen_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=clock.IST)
        age = at - seen_at
        return prices[symbol] if timedelta(seconds=-60) <= age <= timedelta(
            minutes=FRESH_MINUTES) else None

    seen: set[tuple[str, str]] = set()
    results = []
    batch_entries, batch_loss = 0, 0.0

    for number, order in enumerate(orders, 1):
        if not isinstance(order, dict):
            raise GateError(f"order {number} is not an order (a JSON object), got {order!r}")
        reasons, warnings, info, later = [], [], [], []
        symbol = str(order.get("symbol") or "").strip().upper()
        side = str(order.get("side") or "").strip().lower()
        kind_of = str(order.get("order_type") or "MARKET").strip().upper()
        validity = str(order.get("validity") or "DAY").strip().upper()
        product = str(order.get("product") or "MIS").strip().upper()
        exchange = str(order.get("exchange") or "NSE").strip().upper()
        quantity = order.get("quantity")
        plan = order.get("plan") if isinstance(order.get("plan"), dict) else {}
        legs = order.get("stop_loss_price") is not None or order.get("take_profit_price") is not None
        order_class = str(order.get("order_class") or "").strip().lower()
        bracket = order_class == "bracket" or legs
        this_print = fingerprint(order)
        # This order's own record among today's, when re-checking what this gate already passed:
        # it is not a second trade, so it must not count against its own trade or loss limit.
        own = [r for r in entries_today if this_print in done_prints
               and r.get("fingerprint") == this_print]
        entries_before = len(entries_today) - len(own)
        loss_before = loss_today - sum(_num(r.get("max_loss")) or 0.0 for r in own)

        # format
        if not symbol:
            reasons.append("format: no symbol")
        if side not in SIDES:
            reasons.append(f"format: side must be buy or sell, got {order.get('side')!r}")
        q = _num(quantity)
        if q is None or q < 1:
            reasons.append(f"format: quantity must be at least one share, got {quantity!r}")
        elif abs(q - round(q)) > 1e-9:
            reasons.append(f"format: Indian equities trade in whole shares only, got {quantity!r}")
        if kind_of not in ORDER_TYPES:
            reasons.append(f"format: unknown order type {kind_of!r} (MARKET, LIMIT, SL or SL-M)")
        if validity not in VALIDITIES:
            reasons.append(f"format: unknown validity {validity!r} (DAY, IOC or TTL)")
        if product not in PRODUCTS:
            reasons.append(f"format: unknown product {product!r} (MIS, CNC, NRML or MTF)")
        if exchange not in EXCHANGES:
            reasons.append(f"format: unknown exchange {exchange!r} (NSE or BSE; NFO or BFO for "
                           f"derivatives)")
        if kind_of in ("LIMIT", "SL") and _num(order.get("price")) is None:
            reasons.append(f"format: a {kind_of} order needs a price")
        if kind_of in ("SL", "SL-M") and _num(order.get("trigger_price")) is None:
            reasons.append(f"format: a {kind_of} order needs a trigger_price")
        for field in ("price", "trigger_price", "stop_loss_price", "take_profit_price"):
            if order.get(field) is not None and (_num(order[field]) is None or _num(order[field]) <= 0):
                reasons.append(f"format: {field} must be a positive number, got {order[field]!r}")
        if bracket:
            # No both-legs rule, unlike the US pack's bracket: Zerodha has no bracket order, and
            # /nse-broker protects an order with a stop alone (an SL-M, or a single-leg GTT).
            warnings.append(_exits_note(order, product, side))
        if order_class and order_class != "bracket":
            warnings.append(f"advanced order ({order_class}): check every leg before you say yes")

        # asset
        options_on = bool(c.get("allow_options"))
        if symbol and DERIVATIVE.search(symbol):
            if not options_on:
                reasons.append(f"asset: {symbol} is a derivative contract; this pack trades the cash "
                               f"market only (\"allow_options\": false)")
            else:
                warnings.append("derivative order: an option can lose everything paid for it")
                if exchange not in DERIVATIVE_EXCHANGES:
                    reasons.append(f"asset: {symbol} is a derivative contract and trades on NFO or "
                                   f"BFO, not {exchange}")
        elif symbol and (not SYMBOL.match(symbol) or NOT_EQUITY.search(symbol)):
            reasons.append(f"asset: {symbol!r} does not look like an NSE equity symbol")
        elif exchange in DERIVATIVE_EXCHANGES and not options_on:
            reasons.append(f"asset: {exchange} is the derivatives segment; this pack trades the cash "
                           f"market only (\"allow_options\": false)")
        if product == "NRML" and not options_on:
            reasons.append("product: NRML is the derivatives product; this pack trades the cash "
                           "market only (\"allow_options\": false)")
        if product == "MTF" and not c.get("allow_margin"):
            later.append("product: MTF is margin trading funding, a position bought partly with "
                         "borrowed money (\"allow_margin\": false)")

        # the price one share costs to get in: a limit's own price, otherwise the HIGHER of the
        # plan's entry and a fresh quote, so a plan written at 10:15 cannot understate the risk at
        # 10:55 after the price has run
        plan_entry = _num(plan.get("entry"))
        live = fresh(symbol)
        ref, ref_from = None, None
        if kind_of in ("LIMIT", "SL"):
            ref, ref_from = _num(order.get("price")), "the limit price"
        elif kind_of == "SL-M":
            ref, ref_from = _num(order.get("trigger_price")), "the trigger price"
        if ref is None:
            candidates = [(p, w) for p, w in ((plan_entry, "the plan's entry"),
                                              (live, "the latest price")) if p]
            if candidates:
                ref, ref_from = max(candidates, key=lambda x: x[0])
                # To the paisa, half up, as the US pack takes it to the cent: a quote of 961.855
                # is paid as 961.86, and the value, the cash and the risk all follow from that.
                ref = _paisa(ref)
                if live and plan_entry and live > plan_entry + PAISA:
                    info.append(f"{symbol} last {ref:,.2f} is above the plan's entry "
                                f"{plan_entry:,.2f}: the risk is measured from {ref:,.2f}")
            elif symbol in prices:
                ref, ref_from = prices[symbol], "an old price"
                warnings.append(f"the only {symbol} price is not a fresh one (none from the last "
                                f"{FRESH_MINUTES} minutes with its own quote time): the figures "
                                f"below are measured from it; refresh it with /nse-alerts quote "
                                f"{symbol} --write")
        if plan_entry and live is None and symbol in prices and kind_of == "MARKET":
            warnings.append(f"the {symbol} price in prices.json is not from the last "
                            f"{FRESH_MINUTES} minutes (or has no quote time of its own), so it "
                            f"was not used")
        value = q * ref if (q and ref) else None

        # tick: the exchange refuses a price that is not a whole number of ticks. The tick depends
        # on the stock's price (clock.tick_size), so it is judged against the entry where known.
        from nsetools import clock as _clock
        for field in ("price", "trigger_price", "stop_loss_price", "take_profit_price"):
            given = _num(order.get(field))
            if given and given > 0 and _off_tick(given, ref or given):
                tick = _clock.tick_size(ref or given)
                reasons.append(f"tick: {field} {given:,.2f} is not a multiple of the Rs {tick:.2f} "
                               f"tick; the exchange would refuse it")

        # entry or exit
        held = acct["shares"].get(symbol, 0.0)
        held_value = acct["values"].get(symbol, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9 and q is not None:
            exiting = q <= held + 1e-9
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append(f"price: no price for {symbol} to size {int(q)} shares against; add one "
                         f"with /nse-alerts quote {symbol} --write")

        # blocked list and size
        if symbol in blocked:
            later.append(f"blocked: {symbol} is on your blocked list")
        if value is not None:
            if least_order and value < least_order - PAISA:
                later.append(f"size: {_rupees(value)} is under your {_rupees(least_order)} "
                             f"minimum order")
            if most_order and value > most_order + PAISA:
                later.append(f"size: {_rupees(value)} is over your {_rupees(most_order)} max order")

        # what the account would look like, in order, as if each earlier PASS had filled
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer = total * buffer_pct / 100
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -PAISA and not c.get("allow_margin"):
                later.append(f"cash: needs {_rupees(value)} with only {_rupees(cash)} in cash; that "
                             f"would be margin, borrowed money")
            elif after_cash < -PAISA:
                # allow_margin lifts the margin refusal only; the buffer still holds, as in the US
                # pack. Said as it is: "leaves Rs 0.00" would hide that the cash runs out.
                later.append(f"cash: needs {_rupees(value)} with only {_rupees(cash)} in cash; "
                             f"\"allow_margin\" is on, but the buy would still leave less than "
                             f"your {buffer_pct:g}% buffer of {_rupees(buffer)}")
            elif after_cash < buffer - PAISA:
                later.append(f"cash: leaves {_rupees(max(after_cash, 0.0))}, under your "
                             f"{buffer_pct:g}% buffer of {_rupees(buffer)}")
            cap = total * position_pct / 100
            if total > 0 and held_value + value > cap + PAISA:
                later.append(f"position: {symbol} would be {_rupees(held_value + value)}, "
                             f"{(held_value + value) / total * 100:.1f}% of a {_rupees(total)} "
                             f"account, over your {position_pct:g}% cap")
        if side == "sell" and symbol and kind == "entry":
            over = q > held + 1e-9 if q is not None else held <= 0
            if over and not c.get("allow_short"):
                reasons.append(f"short: selling more {symbol} than you hold ({_count(held)} shares); "
                               f"short selling is off (\"allow_short\": false). If you bought it, "
                               f"record the fill first (/nse-broker: ticket.py fill)")
        # How many stocks at once. A buy of a stock not yet held, or a short of one, opens a new
        # position; adding to one already open does not.
        opens_new = abs(acct["shares"].get(symbol, 0.0)) <= 1e-9
        if cap_positions and kind == "entry" and side in SIDES and opens_new:
            open_now = sum(1 for n in acct["shares"].values() if abs(n) > 1e-9)
            if open_now >= cap_positions:
                later.append(f"positions: {open_now} already open, your limit is {cap_positions} "
                             f"(\"max_positions\")")

        # the day-trade checks
        max_loss = risk_ps = reward_risk = stop = target = None
        if kind == "entry" and side in SIDES:
            long = side == "buy"
            leg_stop, plan_stop = _num(order.get("stop_loss_price")), _num(plan.get("stop"))
            stop = leg_stop or plan_stop
            target = _num(order.get("take_profit_price")) or _num(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > PAISA:
                warnings.append(f"the order's stop {leg_stop:,.2f} differs from the plan's "
                                f"{plan_stop:,.2f}; the order's is the one that counts")
            if stop and not leg_stop:
                warnings.append(f"the stop {stop:,.2f} is in the plan only, not at the broker: set a "
                                f"stop alert (/nse-alerts: alerts.py add --from-plan)")
            if not stop:
                if c.get("require_stop", True):
                    later.append('stop: no stop. Every entry needs one ("require_stop": true): '
                                 "set it with /nse-exit and size it with /nse-budget")
                else:
                    warnings.append("no stop: the loss is not capped, so it cannot be counted in "
                                    "the daily loss")
            elif ref:
                if (long and stop >= ref - PAISA) or (not long and stop <= ref + PAISA):
                    later.append(f"stop: the stop {stop:,.2f} must be "
                                 f"{'below' if long else 'above'} the entry {ref:,.2f}")
                else:
                    # In whole paise, as the US pack works in whole cents, so a float like
                    # 11.799999 cannot shift a figure at a limit.
                    risk_paise = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_paise / 100.0
                    distance = risk_ps / ref * 100
                    # The Indian pair the US pack does not have: on an Indian mid cap a band under
                    # 1% sits inside one minute's range, and one past 25% is a different bet.
                    # Compared in whole paise: 965.00 to 955.35 is exactly 1%, but as floats
                    # it is 0.9999999999999976% and was blocked as under a 1% floor.
                    if floor and risk_paise * 100 < round(floor * round(ref * 100), 6):
                        later.append(f"stop: {distance:.2f}% from the entry is inside your "
                                     f"{floor:g}% floor; on an Indian mid cap that is one "
                                     f"minute's noise")
                    if distance > MAX_STOP_PERCENT:
                        later.append(f"stop: {distance:.1f}% from the entry is beyond "
                                     f"{MAX_STOP_PERCENT:g}%, a different size of bet")
                    # No readable quantity, no loss figure: a "max loss Rs 0.00" for an order of
                    # "nan" shares would be a number made up. The format reason blocks it.
                    shares = q if q is not None and q > 0 else None
                    max_loss = round(shares * risk_ps, 2) if shares else None
                    if shares and per_trade and max_loss > per_trade + PAISA:
                        later.append(f"loss: {_count(shares)} shares x {risk_ps:,.2f} risk a share = "
                                     f"{_rupees(max_loss)} at the stop, over your "
                                     f"{_rupees(per_trade)} max loss per trade")
                    if target:
                        if (long and target <= ref + PAISA) or (not long and target >= ref - PAISA):
                            later.append(f"target: the target {target:,.2f} must be "
                                         f"{'above' if long else 'below'} the entry {ref:,.2f}")
                        else:
                            reward_risk = round(int(round(abs(target - ref) * 100))
                                                / float(risk_paise), 2)
                            if least_rr and reward_risk < least_rr - 1e-9:
                                later.append(f"reward:risk: {reward_risk:.2f} (target {target:,.2f}, "
                                             f"entry {ref:,.2f}, stop {stop:,.2f}) is under your "
                                             f"minimum {least_rr:g}")
                    elif least_rr:
                        later.append(f"reward:risk: no target to measure it against "
                                     f"(min_reward_risk {least_rr:g})")
            used = loss_before + batch_loss
            if per_day and max_loss is not None:
                if used + max_loss > per_day + PAISA:
                    later.append(f"daily loss: {_rupees(used)} already at risk today plus "
                                 f"{_rupees(max_loss)} for this one is {_rupees(used + max_loss)}, "
                                 f"over your {_rupees(per_day)} daily limit")
            if cap_trades and entries_before + batch_entries >= cap_trades:
                later.append(f"trades: {entries_before} today plus {batch_entries} passed "
                             f"earlier in this batch; your limit is {cap_trades} a day")
            later.extend(window_reasons)
            if stop and ref and risk_ps:
                info.append(f"entry {ref:,.2f} ({ref_from}), stop {stop:,.2f}, target "
                            f"{f'{target:,.2f}' if target else 'none'}: risk {risk_ps:,.2f} a share")
            if stop and ref and risk_ps and max_loss is not None:
                info.append(f"max loss {_rupees(max_loss)}"
                            f"{f' (limit {_rupees(per_trade)})' if per_trade else ''} at the stop"
                            f"{_with_costs(max_loss, value or 0, q * stop, long, product, exchange)}"
                            f"; reward:risk "
                            f"{f'{reward_risk:.2f}' if reward_risk is not None else 'n/a'}"
                            f"{f' (min {least_rr:g})' if least_rr else ''}")
            if per_day or cap_trades:
                info.append(f"today before this: {entries_before + batch_entries} of "
                            f"{cap_trades or 'unlimited'} trades, {_rupees(used)} of "
                            f"{_rupees(per_day) if per_day else 'no limit on'} daily loss")

        # duplicates and the daily order count
        key = (symbol, side)
        if key in seen:
            later.append(f"duplicate: a second {side} {symbol} in this batch")
        if key in placed_keys and this_print not in done_prints:
            later.append(f"duplicate: a {side} {symbol} already went out today")
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        already = len(done) - (1 if this_print in done_prints else 0)
        if cap_orders and already + passed_so_far >= cap_orders:
            later.append(f"daily limit: {already} today plus {passed_so_far} passed earlier in this "
                         f"batch; your limit is {cap_orders} orders a day")
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warnings.extend("exit, not blocked: " + reason for reason in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and this_print not in done_prints:
                batch_entries += 1
                batch_loss += max_loss or 0.0
            # Shares move whenever the quantity is known, priced or not, as the US pack does: two
            # unpriced sells of 10 from 15 held must not both count as exits.
            if side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][symbol] = acct["values"].get(symbol, 0.0) + value
                if q is not None:
                    acct["shares"][symbol] = held + q
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][symbol] = max(acct["values"].get(symbol, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][symbol] = held - q
        results.append({
            "index": number, "symbol": symbol, "side": side, "kind": kind,
            # As given: int(4.5) would print a refused 4.5-share order as "4 shares".
            "quantity": (int(q) if q is not None and q == int(q) else quantity),
            "value_rupees": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "reward_risk": reward_risk, "fingerprint": this_print, "verdict": verdict,
            "reasons": reasons, "warnings": warnings, "info": info,
        })

    return {
        "date": today, "now_ist": at.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)), "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn, "cash_after": round(acct["cash"], 2),
        "today": {"orders_out": len(done), "entries": len(entries_today),
                  "loss_at_risk": round(loss_today, 2),
                  "counted_from": "the broker's log" if from_broker else "this gate's passes"},
    }


def report(res: dict) -> str:
    lines = [f"WARNING: {w}" for w in res["warnings"]]
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        amount = f"{r['quantity']} shares" if r["quantity"] is not None else "no quantity"
        lines.append(f"{r['verdict']:<5} #{r['index']} {r['side']} {r['symbol']} {amount}, "
                     f"{r['kind']}")
        lines += [f"          {x}" for x in r["info"]]
        lines += [f"        - {x}" for x in r["reasons"]]
        lines += [f"        ~ {x}" for x in r["warnings"]]
    lines.append("")
    lines.append(f"{res['pass']} passed, {res['block']} blocked at {res['now_ist']} IST. Cash after "
                 f"the passed orders: {_rupees(res['cash_after'])} "
                 f"({'paper' if res['paper'] else 'LIVE'} account).")
    lines.append("Blocked orders must not go to the broker. Fix the order or your rules, then check "
                 "again." if res["block"] else
                 "Every order passed. Nothing has been placed; you still decide each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def _read(path: Path | None, what: str, required: bool = False):
    if path is None or not path.exists():
        if required:
            raise GateError(f"no {what} at {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise GateError(f"{path} is not valid JSON: {err}") from err
    except OSError as err:
        raise GateError(f"{path} cannot be read: {err}") from err


def record(res: dict, out: Path, orders_file: Path) -> Path:
    """Keep today's verdicts, by fingerprint, so a later step can tell THIS order passed today."""
    from datetime import datetime, timezone
    previous = _read(out, "risk state") or {}
    previous = previous if isinstance(previous, dict) else {}
    passed = previous.get("passed", []) if previous.get("date") == res["date"] else []
    passed = [p for p in passed if isinstance(p, dict)] if isinstance(passed, list) else []
    have = {p.get("fingerprint") for p in passed}
    for r in res["results"]:
        if r["verdict"] == "PASS" and r["fingerprint"] not in have:
            passed.append({"fingerprint": r["fingerprint"], "symbol": r["symbol"],
                           "side": r["side"], "kind": r["kind"], "max_loss": r["max_loss"],
                           "quantity": r["quantity"]})
            have.add(r["fingerprint"])
    doc = dict(res)
    doc["passed"] = passed
    doc["checked_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    doc["orders_file"] = str(orders_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Written beside and swapped in, as the US pack does: /nse-broker never reads half a file.
    import os
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orders", type=Path, help="the orders to check (default nse/orders.json)")
    ap.add_argument("--order", dest="orders", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--from-order", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--config", type=Path, help="your rules (default config.json)")
    ap.add_argument("--portfolio", type=Path,
                    help="your account (default nse/portfolio.json); if missing, a fresh account "
                         "funded with your capital")
    ap.add_argument("--prices", type=Path, help="fresh prices (default nse/prices.json)")
    ap.add_argument("--state", type=Path,
                    help="what went out today (default nse/state/broker.json if it exists)")
    ap.add_argument("--now", help="pin the clock, like '2026-10-22 10:20' (IST)")
    ap.add_argument("--today", help="the day the limits count from, YYYY-MM-DD (default today)")
    ap.add_argument("--out", type=Path, help="where the verdicts go (default nse/state/risk.json)")
    ap.add_argument("--no-write", action="store_true", help="check without recording the verdicts")
    ap.add_argument("--online", action="store_true",
                    help="ask the exchange for the holiday list (needs the network); by default "
                         "the built-in list is used and nothing is fetched")
    ap.add_argument("--offline", action="store_true",
                    help="the built-in holiday list (the default; kept so older commands still work)")
    ap.add_argument("--json", action="store_true")
    # Sizing a single idea from scratch, kept from this skill's first version.
    ap.add_argument("--entry", type=show.number, help="size one idea: the price you would pay")
    ap.add_argument("--stop", type=show.number, help="size one idea: where you would admit you were wrong")
    ap.add_argument("--target", type=show.number, help="size one idea: optional target")
    ap.add_argument("--capital", type=show.number, help="size one idea: overrides config.json")
    ap.add_argument("--risk", type=show.number, help="size one idea: percent of capital at risk")
    ap.add_argument("--open-positions", type=int, default=0, help="size one idea: how many you hold")
    args = ap.parse_args(argv)

    from nsetools import clock, settings as rules_file

    if args.entry is not None:
        return _size_one(args)

    try:
        conf = rules_file.load(args.config)
        if args.today and not _re.match(r"^\d{4}-\d{2}-\d{2}$", args.today):
            raise GateError("--today wants a date like 2026-10-22")
        at = clock.now(args.now)
        if args.today:
            from datetime import date
            try:
                day = date.fromisoformat(args.today)
            except ValueError as err:
                raise GateError(f"--today {args.today} is not a real date ({err})") from err
        # --today moves only the day the limits count from, as the US pack's does. The clock
        # stays where it is: the window and how fresh a price is are judged on the real time.
        orders_file = args.orders or show.shared("orders.json")
        orders = load_orders(orders_file)
        # A file named on the command line has to exist, as --config has to: a typo in
        # --portfolio would otherwise check against a fresh account, in --prices against no fresh
        # price, in --state against a day with nothing placed. The defaults may be missing.
        for flag, named in (("--portfolio", args.portfolio), ("--prices", args.prices),
                            ("--state", args.state)):
            if named is not None and not named.exists():
                raise FileNotFoundError(f"no file at {named} ({flag})")
        portfolio = _read(args.portfolio or show.shared("portfolio.json"), "portfolio")
        prices_file = args.prices or show.shared("prices.json")
        prices, times = load_prices(prices_file)
        broker_path = args.state or (show.shared("state") / "broker.json")
        broker_state = _read(broker_path, "broker state")
        # Ignored, today's placed orders would go uncounted against every day limit, so a log
        # that is not an object, whose "orders" is not a list, or that holds a record that is not
        # an object, is refused rather than read around.
        logged = broker_state.get("orders") if isinstance(broker_state, dict) else None
        if broker_state is not None and (
                not isinstance(broker_state, dict)
                or ("orders" in broker_state and not isinstance(logged, list))
                or (isinstance(logged, list) and not all(isinstance(r, dict) for r in logged))):
            raise GateError(f"{broker_path} is not a broker log: it must be a JSON object with "
                            f"an \"orders\" list of records")
        out = args.out or (show.shared("state") / "risk.json")
        risk_state = _read(out, "risk state")
        if risk_state is not None and (not isinstance(risk_state, dict) or (
                "passed" in risk_state and not isinstance(risk_state.get("passed"), list))):
            # With no broker log, today's limits count this gate's own passes from this file.
            raise GateError(f"{out} is not this gate's verdicts file: it must be a JSON object "
                            f"with a \"passed\" list; move it aside to start the day's count afresh")
        res = check(orders, conf, portfolio, prices, times, broker_state, risk_state, at,
                    offline=not args.online, today=args.today)
        stamped = _read(prices_file, "prices")
        if isinstance(stamped, dict) and stamped.get("as_of"):
            res["prices_as_of"] = stamped["as_of"]
        if not args.no_write:
            res["recorded"] = str(record(res, out, orders_file))
    except FileNotFoundError as err:
        print(f"BLOCK\n\n   - {err}. A file named on the command line has to exist: falling "
              f"back to defaults would quietly drop what you asked for.")
        return 1
    except (GateError, rules_file.BadConfig, clock.BadTime) as err:
        # A config that cannot be read, or a --now that cannot be, is exit 1, as the US pack's
        # check.py exits on either.
        print(f"risk.py: {err}", file=sys.stderr)
        return 1
    except OSError as err:
        print(f"risk.py: {err}", file=sys.stderr)
        return 1

    print(json.dumps(res, indent=2, default=str) if args.json else report(res))
    return 1 if res["block"] else 0


def _size_one(args) -> int:
    """The original single-idea sizing mode: --entry and --stop, PASS or BLOCK, exit 0 or 1."""
    from nsetools.settings import BadConfig
    try:
        conf = settings(args.config)
    except FileNotFoundError as err:
        print(f"BLOCK\n\n   - {err}. A config named with --config has to exist: falling back to the "
              f"default limits here would quietly drop the ones you asked for.")
        return 1
    except (GateError, BadConfig) as err:
        print(f"risk.py: {err}", file=sys.stderr)
        return 1
    for flag, given in (("--capital", args.capital), ("--risk", args.risk)):
        if given is not None and given <= 0:
            # 0 or less is no capital and no risk anyone meant; read as one, it sized a negative
            # number of shares and called that "the smallest position risks more than allowed".
            print(f"risk.py: {flag} must be above 0, got {given:g}", file=sys.stderr)
            return 1
    if args.capital is not None:
        conf["capital"] = args.capital
        conf["_capital_default"] = False
    if args.risk is not None:
        conf["risk_per_trade_percent"] = args.risk
    if args.stop is None:
        print("--stop is needed with --entry")
        return 2
    out = size(args.entry, args.stop, conf, args.target, args.open_positions)
    # Said in the machine-readable form too: a caller reading the JSON must not take the default
    # capital for the user's own.
    out["capital_is_default"] = bool(conf.get("_capital_default"))
    if args.json:
        print(json.dumps(out, indent=2))
        return 0 if out["verdict"] == "PASS" else 1
    print(f"{out['verdict']}\n")
    if out["verdict"] == "BLOCK":
        for reason in out["reasons"]:
            print(f"   - {reason}")
        if conf.get("_capital_default"):
            print(f"\n   Checked against the default capital of {_rupees(conf['capital'])}: there "
                  f"is none in your config. Set capital in config.json, or pass --capital.")
        print("\nNothing sized. Change the stop, the capital or the limit and ask again.")
        return 1
    print(f"   shares                {out['shares']:,}")
    print(f"   position              {out['position_value']:,.2f}  "
          f"({out['position_percent_of_capital']}% of capital)")
    print(f"   stop                  {out['stop']:,.2f}  ({out['stop_percent']}% away)")
    print(f"   risk budget           {out['risk_budget']:,.2f}")
    print(f"   costs, round trip     {out['costs_round_trip']:,.2f}")
    print(f"   loss if stopped       {out['loss_if_stopped']:,.2f}")
    if "target" in out:
        print(f"   gain if target        {out['gain_if_target']:,.2f}")
        print(f"   reward to risk        {out['reward_to_risk']}")
    if conf.get("_capital_default"):
        print(f"\n   No capital in your config, so this is sized against the default "
              f"{_rupees(conf['capital'])}. Set capital in config.json, or pass --capital.")
    if conf.get("_from"):
        # Only what did come from the file: a --capital or --risk given here is not the file's.
        mine = [name for name, given in (("capital", args.capital), ("risk", args.risk))
                if given is not None]
        print(f"\n   limits read from {conf['_from']}"
              + (f"; {' and '.join(mine)} from the command line" if mine else
                 " (capital included)"))
    for note in out["notes"]:
        print(f"\n   note: {note}")
    print("\nSizing is arithmetic, not a recommendation. This tool cannot place an order.")
    return 0


if __name__ == "__main__":
    show.run(main)
