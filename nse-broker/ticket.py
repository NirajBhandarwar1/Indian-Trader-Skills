#!/usr/bin/env python3
"""Order tickets and the decision log for NSE stocks. Never talks to a broker, never places anything.

It does its work around the one moment that matters, your yes:

  show      renders each proposed order as a plain ticket, plus the exact arguments Claude will
            send to Zerodha's Kite MCP tools. An order is READY only if /nse-risk passed THIS exact
            order today (matched by fingerprint) and nobody has decided on it yet.
  approve   records your explicit yes for ONE order, word for word. It refuses "yes to all",
            "sure", "ok", a yes that names a different stock or amount, and any order not READY.
  decline   records a no.
  result    records what happened after Claude's Allow once / Deny card: placed (with Zerodha's
            order id and the fill), denied, or failed. On a placed entry with a fill it prints the
            protective stop order to place next.
  fill      records a fill in nse/portfolio.json (cash and shares).
  flatten   builds the sell ticket that closes a position by flat-by. It still goes through
            /nse-risk and your yes.
  log       prints today's decisions.

    python ticket.py show                     # every order, READY or not
    python ticket.py show --index 1 --json    # one ticket as JSON
    python ticket.py approve --index 1 --reply "yes"
    python ticket.py result --index 1 --status placed --broker-id 250101000001 --filled-qty 41 --filled-price 961.90
    python ticket.py fill --symbol SBIN --side sell --qty 41 --price 950.05
    python ticket.py flatten --symbol SBIN --qty 41
    python ticket.py log

HOW AN INDIAN DAY TRADE IS PROTECTED. Zerodha has no bracket order (they were withdrawn in 2020),
and a GTT one-cancels-other covers delivery (CNC) positions, not intraday ones. So an intraday
trade goes in three steps, each its own tool call and its own Allow once:
  1. the entry: place_order, regular, MIS;
  2. once it fills, the protective stop: place_order, regular, MIS, SELL, SL-M at the stop;
  3. the target is NOT left at the broker: a resting target sell beside the stop is two sells for
     one position, and if both filled you would be short. /nse-alerts rings at the target; then
     flatten (cancel the stop, sell at market).
A delivery (CNC, or MTF) position is held overnight and an SL-M ends at the close, so it gets a
GTT instead: two-leg with a stop and a target (a true one-cancels-other at Zerodha), single with a
stop alone.

PAPER. Zerodha has no paper account. With "paper": true (the default) nothing is ever sent:
the ticket shows what would be sent, and you record the fill you would have had.

Everything is written to nse/state/broker.json (append only). Dates are IST; --now pins the clock.
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
from nsetools.orders import BROKER_FIELDS, fingerprint           # noqa: E402

# Only "yes" or "y" opens a yes, as in the US pack: the one gate that leads to real money takes no
# looser word ("ha" is as often a laugh as a yes). The Hindi batch and doubt words stay: they only
# ever refuse.
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each",
               "sab"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if", "nahi"}
DEFAULTS = {"paper": True, "flat_by": "15:20"}
# Words a plain yes may carry that are not a stock: "yes please place it", "yes go ahead".
PLAIN_WORDS = {"YES", "Y", "OK", "PLEASE", "PLACE", "IT", "GO", "AHEAD", "BUY",
               "SELL", "ORDER", "THE", "THIS", "ONE", "SHARES", "SHARE", "AT", "MARKET", "DO",
               "CONFIRM", "CONFIRMED", "SURE", "RS", "NOW", "THAT", "FOR", "OF"}


class TicketError(Exception):
    """Something that cannot be done as asked, with the reason."""


# ---------------------------------------------------------------- files

def read_json(path: Path, what: str, required: bool = True):
    if not path.exists():
        if required:
            raise TicketError(f"no {what} at {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise TicketError(f"{path} is not valid JSON: {err}") from err
    except OSError as err:
        raise TicketError(f"cannot read {path}: {err}") from err


def read_object(path: Path, what: str, required: bool = True, lists: tuple = ()) -> dict | None:
    """A JSON object, or a refusal naming the file. `lists` are keys that must hold lists of
    objects when present. A damaged file is never read as empty: that is how a log loses its day
    or a portfolio its shares, and nothing is written over it."""
    doc = read_json(path, what, required)
    if doc is None:
        return None
    if not isinstance(doc, dict):
        raise TicketError(f"{path} is damaged: the {what} must be a JSON object, not a "
                          f"{type(doc).__name__}. Fix it or move it aside; nothing was written")
    for key in lists:
        rows = doc.get(key)
        if rows is None:
            continue
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise TicketError(f"{path} is damaged: \"{key}\" in the {what} must be a list of "
                              f"objects. Fix it or move it aside; nothing was written")
    return doc


def real_date(text: str, flag: str) -> str:
    """A YYYY-MM-DD that is a real calendar day; 2026-02-30 is refused, not filed under it."""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text or ""):
            return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    raise TicketError(f"{flag} wants a real date like 2026-10-22, not {text!r}")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def rec_day(rec: dict) -> str:
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def _f(x):
    try:
        number = float(x)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _num(v) -> str:
    """A quantity as written: 41, 41.5. Something that is not a finite number is shown as it is
    (a damaged order still renders, as NOT READY, rather than stopping the run)."""
    f = _f(v)
    if f is None:
        return repr(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _px(v) -> str:
    """A price to two places, or the raw value when it is not a number."""
    f = _f(v)
    return f"{f:.2f}" if f is not None else repr(v)


def _qty(v):
    """A whole-share quantity for a tool call, or the raw value when it is not one (such an order
    never passes /nse-risk, so it is only ever shown, never sent)."""
    f = _f(v)
    return int(f) if f is not None and f == int(f) else v


def _up(v, default: str) -> str:
    """Zerodha's fields are upper case (MARKET, MIS, DAY, NSE); /nse-risk reads them in any case,
    so the call has to send them the way Zerodha spells them."""
    return str(v or default).strip().upper()


def is_paper(conf: dict) -> bool:
    """Paper unless the setting is exactly false. The config's switch is refused unless it is
    true or false (_config); a log record written without one is read as paper, never live."""
    return conf.get("paper", True) is not False


def _rs(v) -> str:
    text = f"{float(v):,.2f}"
    return "Rs " + (text[:-3] if text.endswith(".00") else text)


# ---------------------------------------------------------------- the order and its tool calls

def is_protected(order: dict) -> bool:
    return order.get("stop_loss_price") is not None or order.get("take_profit_price") is not None


def entry_call(order: dict) -> dict:
    """The exact arguments for Kite MCP place_order. Zerodha's parameter names, numbers as numbers.

    Checked against zerodha/kite-mcp-server (mcp/post_tools.go): variety, exchange, tradingsymbol,
    transaction_type, quantity, product, order_type are required; price for LIMIT; trigger_price
    for SL and SL-M; validity DAY, IOC or TTL. "why" and "plan" are ours and are never sent.
    """
    out = {"variety": "regular", "exchange": _up(order.get("exchange"), "NSE"),
           "tradingsymbol": str(order.get("symbol", "")).upper(),
           "transaction_type": _up(order.get("side"), "buy"),
           "quantity": _qty(order.get("quantity") or 0),
           "product": _up(order.get("product"), "MIS"),
           "order_type": _up(order.get("order_type"), "MARKET"),
           "validity": _up(order.get("validity"), "DAY")}
    if out["order_type"] in ("LIMIT", "SL") and order.get("price") is not None:
        out["price"] = _f(order["price"]) if _f(order["price"]) is not None else order["price"]
    if out["order_type"] in ("SL", "SL-M") and order.get("trigger_price") is not None:
        out["trigger_price"] = (_f(order["trigger_price"]) if _f(order["trigger_price"])
                                is not None else order["trigger_price"])
    return out


def gtt_stop_limit(stop: float) -> float:
    """The limit price of a GTT's stop leg: 1% below the trigger, on a tick.

    A GTT leg is a LIMIT order once it triggers (Zerodha has no market GTT). A limit AT the stop
    sits unfilled the moment the price gaps through it, which is exactly when a stop is needed;
    1% below gives it room. A gap past that can still leave it unfilled, and the ticket says so.
    """
    return clock.to_tick(stop * 0.99, up=False)


def protection_call(order: dict, quantity=None, last_price=None) -> tuple[str, dict] | None:
    """What protects the position once the entry fills: (tool, arguments), or None.

    Intraday (MIS) long: an SL-M SELL at the stop. Intraday (MIS) short: an SL-M BUY at the stop.
    Both for the quantity filled. A position held overnight (CNC, or MTF) is protected by a GTT,
    because an SL-M is a DAY order and is gone at the close while the shares are not: two-leg
    (Zerodha's one-cancels-other) with a stop and a target, single with a stop alone. A CNC short
    does not exist in Indian cash equity.
    """
    if not is_protected(order):
        return None
    side = str(order.get("side", "")).lower()
    qty = _qty(quantity if quantity is not None else order.get("quantity") or 0)
    symbol = str(order.get("symbol", "")).upper()
    product = _up(order.get("product"), "MIS")
    exchange = _up(order.get("exchange"), "NSE")
    stop, target = _f(order.get("stop_loss_price")), _f(order.get("take_profit_price"))
    if side == "buy" and product in ("CNC", "MTF") and stop:
        args = {"exchange": exchange, "tradingsymbol": symbol}
        # last_price is the price now, which Zerodha checks the triggers against. Never a made-up
        # 0: with no price known the key is left out and the words say to fill it in.
        last = _f(last_price) or _f((order.get("plan") or {}).get("entry"))
        if last:
            args["last_price"] = last
        args.update({"transaction_type": "SELL", "product": product})
        if target:
            args.update({"trigger_type": "two-leg",
                         "upper_trigger_value": target, "upper_quantity": qty,
                         "upper_limit_price": target,
                         "lower_trigger_value": stop, "lower_quantity": qty,
                         "lower_limit_price": gtt_stop_limit(stop)})
        else:
            args.update({"trigger_type": "single", "trigger_value": stop, "quantity": qty,
                         "limit_price": gtt_stop_limit(stop)})
        return "place_gtt_order", args
    if not stop or side not in ("buy", "sell") or (side == "sell" and product != "MIS"):
        return None
    return "place_order", {"variety": "regular", "exchange": exchange,
                           "tradingsymbol": symbol,
                           "transaction_type": "SELL" if side == "buy" else "BUY",
                           "quantity": qty, "product": product, "order_type": "SL-M",
                           "trigger_price": stop, "validity": "DAY"}


def protection_words(tool: str, args: dict) -> list[str]:
    """What the protection does, in plain words, beside its JSON."""
    if tool == "place_gtt_order":
        two = args.get("trigger_type") == "two-leg"
        trigger = args["lower_trigger_value"] if two else args["trigger_value"]
        limit = args["lower_limit_price"] if two else args["limit_price"]
        words = [f"  The stop {'leg ' if two else ''}triggers at {trigger:.2f} and then places a "
                 f"LIMIT sell at {limit:.2f} (1% below; a GTT has no market order). If the price "
                 f"gaps below {limit:.2f}, the sell can stay unfilled. You can change that limit "
                 f"before it is placed."]
        if not two:
            words.append("  There is no target leg: the order has no target, so only the stop "
                         "rests at Zerodha.")
        if "last_price" not in args:
            words.append("  last_price is left out because no price is known here: add the price "
                         "now (/nse-alerts quote) before placing it.")
        return words
    return ["  The target is not left at Zerodha: a resting target order beside the stop is two "
            "orders for one position. /nse-alerts rings at the target; then ticket.py flatten."]


def max_loss(order: dict, verdict: dict | None = None) -> tuple[float | None, float | None]:
    """(rupees, stop) lost if the stop fills exactly at the stop, or (None, stop).

    Where /nse-risk passed this order, its own figure is the one used: the gate measures a market
    entry from the HIGHER of the plan's entry and a fresh quote, so after the price has run its
    figure is the larger, true one, and it is the figure the daily loss limit counts. Otherwise the
    gate's rule is followed here: a limit (LIMIT, SL) from its price, an SL-M from its trigger, a
    market order from the plan.
    """
    plan = order.get("plan") if isinstance(order.get("plan"), dict) else {}
    stop = _f(order.get("stop_loss_price")) or _f(plan.get("stop"))
    if verdict and _f(verdict.get("max_loss")) is not None and stop \
            and _f(verdict.get("stop")) is not None and abs(_f(verdict["stop"]) - stop) < 0.005:
        return round(_f(verdict["max_loss"]), 2), stop
    qty = _f(order.get("quantity"))
    if not stop or not qty:
        return None, stop
    kind = _up(order.get("order_type"), "MARKET")
    if kind in ("LIMIT", "SL") and _f(order.get("price")):
        entry = _f(order["price"])
    elif kind == "SL-M" and _f(order.get("trigger_price")):
        entry = _f(order["trigger_price"])
    elif _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    return round(qty * int(round(abs(entry - stop) * 100)) / 100.0, 2), stop


VALIDITY_WORDS = {"DAY": "good for today", "IOC": "immediate or cancel",
                  "TTL": "valid for a set number of minutes (TTL)"}


def plain_parts(order: dict, verdict: dict | None = None) -> list[str]:
    side = str(order.get("side", "")).upper()
    symbol = str(order.get("symbol", "?")).upper()
    kind = _up(order.get("order_type"), "MARKET")
    if kind == "LIMIT" and order.get("price") is not None:
        how = f"limit {_px(order['price'])}"
    elif kind == "SL":
        how = f"stop-limit, trigger {_px(order.get('trigger_price'))}, limit {_px(order.get('price'))}"
    elif kind == "SL-M":
        how = f"stop-market, trigger {_px(order.get('trigger_price'))}"
    else:
        how = kind.lower()
    code = _up(order.get("product"), "MIS")
    product = {"MIS": "intraday (MIS)", "CNC": "delivery (CNC)",
               "MTF": "margin trading (MTF)"}.get(code, code)
    validity = _up(order.get("validity"), "DAY")
    parts = [f"{side} {_num(order.get('quantity') or 0)} {symbol}, {how}, {product}, "
             f"{VALIDITY_WORDS.get(validity, validity)}"]
    if order.get("stop_loss_price") is not None:
        parts.append(f"stop-loss {_px(order['stop_loss_price'])}")
    if order.get("take_profit_price") is not None:
        parts.append(f"target {_px(order['take_profit_price'])}")
    loss, stop = max_loss(order, verdict)
    if loss is not None and is_protected(order):
        parts.append(f"max loss about {_rs(loss)} if the stop fills at {stop:.2f} (it can fill "
                     f"worse in a fast market, or not at all on a circuit)")
    return parts


def plain(order: dict, verdict: dict | None = None) -> str:
    return " / ".join(plain_parts(order, verdict))


def amount_numbers(order: dict, verdict: dict | None = None) -> list[float]:
    nums = [v for v in (_f(order.get(k)) for k in ("quantity", "price", "stop_loss_price",
                                                     "take_profit_price", "trigger_price"))
            if v is not None]
    loss, _ = max_loss(order, verdict)          # the figure the ticket shows, and only that one
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply: str, order: dict, batch_symbols=(), verdict: dict | None = None
              ) -> tuple[bool, str]:
    """Is this reply an explicit yes for THIS one order? The US pack's rule, word for word."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    symbol = str(order.get("symbol", "")).upper()
    tokens = set(re.findall(r"[A-Z][A-Z0-9&\-]{1,19}", raw.upper()))
    named = tokens & ({s.upper() for s in batch_symbols} - {symbol})
    # Stricter than the US pack, which checks only the other symbols in the same batch: "yes ITC"
    # for an SBIN order names a different stock whether or not ITC is in the batch. So every word
    # must be a plain word, this order's own stock, or a number; anything else is refused.
    stray = {w.upper() for w in re.findall(r"[A-Za-z][A-Za-z0-9&-]*", raw)} - PLAIN_WORDS - {symbol}
    stray -= {w.upper() for w in YES_WORDS}
    if stray:
        return False, (f"the reply has {', '.join(sorted(stray))} in it, which is not this "
                       f"order ({symbol}); reply with a plain yes for this one order")
    if named:
        return False, f"the reply names {', '.join(sorted(named))}, but this order is for {symbol}"
    # "yes sell" for a buy order says the opposite of the ticket, so it is not a yes for it.
    side = str(order.get("side", "")).lower()
    other = {"buy": "SELL", "sell": "BUY"}.get(side)
    if other and other.lower() in words:
        return False, (f"the reply says {other.lower()}, but this order is a {side}: "
                       f"{plain(order, verdict)}")
    # Every number has to be one of this order's own (stricter than the US pack, which accepts the
    # reply when any one number matches): "yes 41 50" for a 41-share order is not a yes for it.
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order, verdict)
        wrong = [n for n in nums if not any(abs(n - w) < 0.005 for w in want)]
        if wrong:
            return False, (f"the reply says {', '.join(_num(n) for n in wrong)}, but this order "
                           f"is {plain(order, verdict)}")
    return True, f"explicit yes for {plain(order, verdict)}"


# ---------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    """The broker log. A damaged one is refused, never read as empty: appending to an empty log
    over a damaged one would erase the day's placed orders, which /nse-risk counts."""
    state = read_object(path, "broker state", required=False, lists=("orders",))
    if state is None:
        return {"orders": []}
    state.setdefault("orders", [])
    return state


def latest(state: dict, fp: str, today: str) -> dict | None:
    """The latest decision today for this order. A fill is not a decision, so it is skipped."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today and rec.get("status") != "filled":
            found = rec
    return found


def order_core(order: dict) -> dict:
    """What a record keeps of the order so a later fill or flatten can rebuild its protection."""
    core = {k: order[k] for k in BROKER_FIELDS if order.get(k) is not None}
    entry = _f((order.get("plan") or {}).get("entry"))
    if entry:
        core["plan"] = {"entry": entry}
    return core


def filled_for(state: dict, fp: str, today: str) -> int:
    """Shares already recorded as filled today for this one order."""
    return sum(int(_f(r.get("quantity")) or 0) for r in state.get("orders", [])
               if r.get("status") == "filled" and r.get("fingerprint") == fp
               and rec_day(r) == today)


def placed_entry(state: dict, symbol: str, today: str, side: str | None = None) -> dict | None:
    """Today's latest placed entry for this symbol (and side, if given), or None."""
    found = None
    for rec in state.get("orders", []):
        if (rec.get("status") == "placed" and rec.get("kind") == "entry"
                and str(rec.get("symbol", "")).upper() == symbol.upper() and rec_day(rec) == today
                and (side is None or str(rec.get("side", "")).lower() == side)):
            found = rec
    return found


def last_delivery_entry(state: dict, symbol: str, today: str) -> dict | None:
    """The latest placed CNC or MTF buy entry for this symbol from before today, or None: a
    holding carried overnight, whose sale has to use the product it was bought with."""
    found = None
    for rec in state.get("orders", []):
        product = _up(rec.get("product") or (rec.get("order") or {}).get("product"), "")
        if (rec.get("status") == "placed" and rec.get("kind") == "entry"
                and str(rec.get("side", "")).lower() == "buy" and product in ("CNC", "MTF")
                and str(rec.get("symbol", "")).upper() == symbol.upper()
                and rec_day(rec) < today):
            found = dict(rec, product=product)
    return found


def check_fill_size(state: dict, rec: dict, qty, today: str) -> int:
    """The shares still to fill on this order; refuses a fill bigger than that."""
    ordered = int(_f(rec.get("quantity")) or 0)
    left = ordered - filled_for(state, rec.get("fingerprint"), today)
    try:
        want = float(qty)
    except (TypeError, ValueError):
        raise TicketError("a fill needs a positive quantity and price, as numbers") from None
    if ordered and want > left:
        raise TicketError(f"cannot record {_num(want) if math.isfinite(want) else qty} filled: "
                          f"order {rec.get('index')} ({rec.get('symbol')}) is for {ordered}, and "
                          f"{ordered - left} of it is already recorded, so at most {left} more can "
                          f"fill")
    return left


def risk_verdict(risk: dict | None, fp: str, today: str):
    if not risk:
        return None, "/nse-risk has not checked these orders (run risk.py)"
    if risk.get("date") != today:
        return None, f"the risk check is from {risk.get('date')}, not today; run risk.py again"
    for result in risk.get("results") or []:
        if result.get("fingerprint") == fp:
            return result, None
    return None, ("this exact order was not in the last risk check (it changed or is new); run "
                  "risk.py again")


def status_of(order: dict, risk: dict | None, state: dict, today: str):
    """(ready, label, the risk result)."""
    fp = fingerprint(order)
    verdict, why = risk_verdict(risk, fp, today)
    if verdict is None:
        return False, "NOT READY: " + why, None
    if verdict.get("verdict") != "PASS":
        reasons = verdict.get("reasons")
        reasons = reasons if isinstance(reasons, list) else [reasons] if reasons else []
        return False, "BLOCKED by /nse-risk: " + "; ".join(str(r) for r in reasons), verdict
    # A sell that is not an exit of shares held is a short, of any product. Not supported: the
    # portfolio records only shares you hold, so neither the fill nor its stop could be tracked.
    # (A CNC short does not exist in Indian cash equity at all.)
    if str(order.get("side", "")).lower() == "sell" and kind_of(order, verdict) == "entry":
        return False, ("NOT READY: short entries are not supported: this pack's portfolio records "
                       "only shares you hold, so the fill and its stop could not be tracked"), verdict
    rec = latest(state, fp, today)
    if rec:
        if rec.get("status") == "approved":
            return False, (f"APPROVED by you at {rec.get('at')}, waiting for the place_order "
                           f"call"), verdict
        return False, f"DONE: {rec.get('status')} at {rec.get('at')}", verdict
    return True, "READY: /nse-risk PASS, waiting for your yes", verdict


def kind_of(order: dict, verdict: dict | None) -> str:
    if verdict and verdict.get("kind"):
        return verdict["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


def entry_protection(order: dict, verdict: dict | None):
    """The protection a ticket shows: only an entry opens a position to protect. A sell that
    closes shares held is not followed by a buy stop, whatever stop the order carries."""
    return protection_call(order) if kind_of(order, verdict) == "entry" else None


# ---------------------------------------------------------------- rendering

def render(order: dict, index: int, total: int, conf: dict, risk, state: dict, today: str):
    ready, label, verdict = status_of(order, risk, state, today)
    paper = is_paper(conf)
    account = "PAPER (nothing is sent: Zerodha has no paper account)" if paper else \
        "LIVE ACCOUNT, REAL MONEY"
    lines = [f"ORDER {index} of {total}, {account}, Zerodha"]
    lines += [f"  {part}" for part in plain_parts(order, verdict)]
    if order.get("why"):
        lines.append(f"  why: {order['why']}")
    lines.append(f"  {label}")
    warnings = (verdict or {}).get("warnings") or []
    for warning in (warnings if isinstance(warnings, list) else [warnings]):
        lines.append(f"  note: {warning}")
    lines.append(f"  {'Would call' if paper else 'Claude will call'} place_order with exactly:")
    lines += [f"    {line}" for line in json.dumps(entry_call(order), indent=2).splitlines()]
    after = entry_protection(order, verdict)
    if after:
        tool, args = after
        lines.append(f"  Once it fills, the protection, {tool} (its own yes is this one, "
                     f"{'paper, so nothing is sent' if paper else 'its own Allow once card'}), "
                     f"with the filled quantity:")
        lines += [f"    {line}" for line in json.dumps(args, indent=2).splitlines()]
        lines += protection_words(tool, args)
        lines.append("  Each fill gets its own stop: a partial fill is protected for what filled, "
                     "and the rest when it fills.")
    if ready:
        lines.append("  Reply \"yes\" to " + ("record this one order on paper (nothing is sent)"
                                               if paper else "place this one order")
                     + ", or \"no\" to skip it.")
        if not paper:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order: dict, index: int, conf: dict, risk, state: dict, today: str) -> dict:
    ready, label, verdict = status_of(order, risk, state, today)
    loss, stop = max_loss(order, verdict)
    after = entry_protection(order, verdict)
    return {"index": index, "ready": ready, "status": label, "plain": plain(order, verdict),
            "fingerprint": fingerprint(order), "paper": is_paper(conf),
            "broker": "zerodha", "tool": "place_order",
            "tool_input": entry_call(order), "protection": (
                {"tool": after[0], "tool_input": after[1]} if after else None),
            "why": order.get("why", ""), "protected": is_protected(order),
            "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def _row_symbol(row: dict) -> str:
    """A position's stock, under any of the names /nse-risk reads it by."""
    return str(row.get("symbol") or row.get("tradingsymbol") or row.get("ticker") or "").upper()


def _row_shares(row: dict) -> float:
    """A position's shares, under any of the names /nse-risk reads them by."""
    for key in ("shares", "quantity", "qty"):
        if row.get(key) is not None:
            return _f(row[key]) or 0.0
    return 0.0


def _held(row: dict, path: Path) -> float:
    """A position's shares, read strictly: a whole, non-negative number, or a refusal naming the
    file. Read loosely, "abc" or nan became 0 and 10.5 became 10, and the next write saved that
    over the real figure."""
    for key in ("shares", "quantity", "qty"):
        if row.get(key) is not None:
            held = _f(row[key])
            if held is None or held < 0 or held != int(held):
                raise TicketError(f"{path} is damaged: {_row_symbol(row) or 'a position'} has "
                                  f"{key} {row[key]!r}, not a whole number of shares. Fix it or "
                                  f"move it aside; nothing was written")
            return held
    return 0.0


def read_portfolio(path: Path) -> dict | None:
    """nse/portfolio.json, or None if there is none. Damaged is refused, never read as empty."""
    pf = read_object(path, "portfolio", required=False, lists=("positions",))
    if pf is not None and _f(pf.get("cash")) is None:
        raise TicketError(f"{path} is damaged: it needs a \"cash\" figure in rupees. Fix it or "
                          f"move it aside; nothing was written")
    return pf


def apply_fill(path: Path, conf: dict, symbol: str, side: str, qty, price, day: str) -> dict:
    """Record a fill in nse/portfolio.json: cash and shares, a cost lot per buy, oldest sold first.

    The shape every skill here reads: {"cash", "positions": [{"symbol", "shares", "cost", "lots"}]}.
    A new file starts from the capital (or budget) in the config, and the result says so:
    `pf["_started_from"]`. With neither, nothing is started, as the US pack refuses: a cash figure
    nobody gave would be shown, and sized against, as if it were the user's.
    """
    try:
        qty, price = float(qty), float(price)
    except (TypeError, ValueError):
        raise TicketError("a fill needs a positive quantity and price, as numbers") from None
    # "nan" and "inf" parse as floats and pass a plain "<= 0" test.
    if not (math.isfinite(qty) and math.isfinite(price)) or qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive quantity and price")
    if qty != int(qty):
        raise TicketError("Indian equities trade in whole shares")
    pf = read_portfolio(path)
    started = None
    if pf is None:
        key = "capital" if conf.get("capital") is not None else "budget"
        given = conf.get(key)
        if given is None:
            raise TicketError(f"no {path} yet and no capital (or budget) in the config to start "
                              f"it from: set \"capital\" in config.json (copy config.example.json), "
                              f"or write {path} with your real cash. Nothing was recorded")
        start = _f(given)
        if not start or start <= 0:
            raise TicketError(f"no {path} yet, and the {key} in {conf.get('_from')} is not a "
                              f"positive number to start it from: {given!r}. Nothing was recorded")
        started = f"your {key} {_rs(start)} in {conf.get('_from')}"
        pf = {"cash": float(start), "positions": []}
    symbol = symbol.upper()
    cost = round(qty * price, 2)
    pos = next((p for p in pf.setdefault("positions", []) if _row_symbol(p) == symbol), None)
    if pos is not None:
        # A position written by hand or by another tool may say "quantity" and carry no lots.
        # Its shares become one opening lot at its known cost (or an unknown one), so a fill
        # never quietly drops the shares or the cost already there.
        held = _held(pos, path)
        lots = pos.get("lots") or []
        if not isinstance(lots, list) or not all(isinstance(lot, dict) for lot in lots):
            raise TicketError(f"{path} is damaged: the {symbol} lots must be a list of objects. "
                              f"Fix it or move it aside; nothing was written")
        lots = list(lots)
        for lot in lots:
            have = _f(lot.get("shares"))
            if have is None or have < 0 or have != int(have):
                raise TicketError(f"{path} is damaged: a {symbol} lot has shares "
                                  f"{lot.get('shares')!r}, not a whole number. Fix it or move it "
                                  f"aside; nothing was written")
        if sum(_f(lot.get("shares")) for lot in lots) > held:
            raise TicketError(f"{path} is damaged: the {symbol} lots add up to more than the "
                              f"{_num(held)} shares it says are held. Fix it or move it aside; "
                              f"nothing was written")
        for key in ("quantity", "qty"):
            pos.pop(key, None)
        pos["shares"] = int(held)
        in_lots = sum(_f(lot.get("shares")) for lot in lots)
        if held - in_lots > 0:
            known = _f(pos.get("cost")) if not lots else None
            lots.insert(0, {"id": f"{symbol}-0", "date": "", "shares": int(held - in_lots),
                            "cost": round(known, 2) if known is not None else None})
        pos["lots"] = lots
    if side == "buy":
        if pos is None:
            pos = {"symbol": symbol, "shares": 0, "cost": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos["lots"]
        lots.append({"id": f"{symbol}-{len(lots) + 1}", "date": day, "shares": int(qty),
                     "cost": cost})
        pos["shares"] = int(pos["shares"] + qty)
        pf["cash"] = round(float(pf["cash"]) - cost, 2)
    else:
        if pos is None or pos["shares"] < qty:
            raise TicketError(f"cannot record selling {_num(qty)} {symbol}: the portfolio holds "
                              f"{_num(pos['shares']) if pos else 0}")
        left = qty
        for lot in sorted(pos["lots"], key=lambda lot: str(lot.get("date") or "")):
            if left <= 0:
                break
            have = _f(lot.get("shares")) or 0.0
            take = min(left, have)
            lot_cost = _f(lot.get("cost"))
            if lot_cost is not None and have:
                lot["cost"] = round(lot_cost - lot_cost / have * take, 2)
            lot["shares"] = int(have - take)
            left -= take
        pos["lots"] = [lot for lot in pos["lots"] if (_f(lot.get("shares")) or 0) > 0]
        pos["shares"] = int(pos["shares"] - qty)
        if pos["shares"] <= 0:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf["cash"]) + cost, 2)
    if pos in pf["positions"]:
        costs = [_f(lot.get("cost")) for lot in pos["lots"]]
        # One lot of unknown cost makes the total unknown; it is not counted as free.
        pos["cost"] = round(sum(costs), 2) if all(c is not None for c in costs) else None
    write_json(path, pf)
    if started:
        pf["_started_from"] = started
    return pf


def log_fill(path: Path, at: datetime, day: str, symbol: str, side: str, qty, price,
             fp: str | None = None) -> None:
    """A fill in the log. `fp` ties it to the placed order it fills, so a second fill of the same
    order can be measured against what is left of it."""
    state = load_state(path)
    rec = {"at": at.isoformat(timespec="seconds"), "date": day, "status": "filled",
           "symbol": symbol.upper(), "side": side, "quantity": int(float(qty)),
           "price": float(price),
           "plain": f"{side.upper()} {_num(qty)} {symbol.upper()} at {float(price):.2f}"}
    if fp:
        rec["fingerprint"] = fp
    state.setdefault("orders", []).append(rec)
    write_json(path, state)


def print_protection(rec: dict, qty, price) -> None:
    """The protective order for the shares just filled, or a plain line saying there is none."""
    order = rec.get("order")
    after = protection_call(order, qty, price) if order else None
    if not after:
        if order and is_protected(order):
            print("No protective order can be built for this fill; place a stop yourself.")
        return
    tool, args = after
    paper = is_paper(rec)
    print(f"Now protect these {_num(qty)} shares: {tool} with exactly (your yes for order "
          f"{rec.get('index')} covers it; "
          f"{'paper, so do not call it' if paper else 'its own Allow once card'}):")
    print(json.dumps(args, indent=2))
    for line in protection_words(tool, args):
        print(line.strip())
    symbol = str(order.get("symbol", "")).upper()
    if tool == "place_order":
        print("Keep the stop's order_id from the response: flatten has to cancel it first "
              "(cancel_order, variety regular).")
        print(f"When the stop fills, record that sell: ticket.py fill --symbol {symbol} --side "
              f"sell --qty Q --price P.")
    else:
        print("Keep the GTT's trigger_id: flatten has to remove it first (delete_gtt_order).")
        print(f"When a leg fills, record that sell: ticket.py fill --symbol {symbol} --side sell "
              f"--qty Q --price P.")


# ---------------------------------------------------------------- commands

def _now(text):
    """The clock, or a one-line refusal (exit 1, as the US pack's) for a time it cannot read."""
    try:
        return clock.now(text)
    except clock.BadTime as err:
        raise TicketError(str(err)) from None


def _clock(a):
    at = _now(getattr(a, "now", None))
    given = getattr(a, "today", None)
    today = real_date(given, "--today") if given else at.date().isoformat()
    return at, today


def _config(a) -> dict:
    try:
        conf = settings.load(a.config, DEFAULTS)
    except (FileNotFoundError, settings.BadConfig) as err:
        raise TicketError(str(err)) from err
    # Live or paper is the one switch that decides whether real money moves. Anything but true or
    # false ("false" as text, null, 0) is refused, as /nse-risk refuses it, rather than guessed.
    if not isinstance(conf.get("paper"), bool):
        raise TicketError(f"\"paper\" in {conf.get('_from')} must be true or false, got "
                          f"{conf.get('paper')!r}. Nothing is shown or sent until it is")
    return conf


def _load(a):
    doc = read_json(a.orders, "orders file (run /nse-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if isinstance(orders, dict) and orders.get("symbol"):
        orders = [orders]                                    # one order written on its own
    if not isinstance(orders, list) or not orders:
        raise TicketError(f"no orders in {a.orders}")
    for number, order in enumerate(orders, 1):
        if not isinstance(order, dict):
            raise TicketError(f"{a.orders} is damaged: order {number} is not an order object "
                              f"({order!r}); run /nse-budget again")
    conf = _config(a)
    risk = read_object(a.risk, "risk check", required=False, lists=("results",))
    state = load_state(a.state)
    at, today = _clock(a)
    return orders, conf, risk, state, today, at


def _pick(orders: list, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError(f"there is no order {index} (there are {len(orders)})")
    return orders[index - 1]


def known_symbols(a) -> list[str]:
    """Every stock you are watching, planning or holding: a yes that names one of them for a
    different order is not a yes for this one."""
    names = []
    for path in (show.shared("watchlist.json"), show.shared("plan.json"),
                 getattr(a, "portfolio", None) or show.shared("portfolio.json")):
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if isinstance(doc, dict):
            names += [h.get("symbol", "") for h in doc.get("hits") or [] if isinstance(h, dict)]
            names += [_row_symbol(p) for p in doc.get("positions") or [] if isinstance(p, dict)]
            if doc.get("symbol"):
                names.append(doc["symbol"])
    return [n for n in names if n]


def cmd_show(a) -> int:
    orders, conf, risk, state, today, _ = _load(a)
    # "is not None": --index 0 is a wrong index to refuse, not a request for every ticket.
    one = a.index is not None
    picks = [(a.index, _pick(orders, a.index))] if one else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, conf, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if one else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), conf, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a) -> int:
    orders, conf, risk, state, today, at = _load(a)
    order = _pick(orders, a.index)
    ready, label, verdict = status_of(order, risk, state, today)
    if not ready:
        raise TicketError(f"order {a.index} is not ready: {label}")
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders] + known_symbols(a),
                        verdict)
    if not ok:
        print(f"Not recorded: {why}. Ask the user again with the ticket for order {a.index}.")
        return 1
    loss, stop = max_loss(order, verdict)
    paper = is_paper(conf)
    state.setdefault("orders", []).append({
        "at": at.isoformat(timespec="seconds"), "date": today, "index": a.index,
        "fingerprint": fingerprint(order), "symbol": str(order.get("symbol", "")).upper(),
        "side": order.get("side"), "quantity": order.get("quantity"),
        "kind": kind_of(order, verdict), "max_loss": loss, "stop": stop,
        "plain": plain(order, verdict), "status": "approved", "reply": a.reply, "paper": paper,
        "broker": "zerodha", "product": _up(order.get("product"), "MIS"),
        "order": order_core(order)})
    write_json(a.state, state)
    print(f"Recorded your yes for order {a.index}: {plain(order, verdict)}.")
    if paper:
        print("PAPER: do NOT call place_order. Zerodha has no paper account, so nothing is sent.")
        print(f"Record what would have happened: ticket.py result --index {a.index} --status "
              f"placed --filled-qty Q --filled-price P (the price now, from /nse-alerts quote).")
    else:
        print("Now call place_order with exactly:")
        print(json.dumps(entry_call(order), indent=2))
        print(f"Claude's card shows Allow once / Deny. Then: ticket.py result --index {a.index} "
              f"--status placed|denied|failed")
    return 0


def cmd_decline(a) -> int:
    orders, conf, risk, state, today, at = _load(a)
    order = _pick(orders, a.index)
    done = latest(state, fingerprint(order), today)
    if done and done.get("status") == "placed":
        # A no after the order went out would read in the log as if it never had.
        raise TicketError(f"order {a.index} was already placed at {done.get('at')}; a no cannot "
                          f"undo it. To get out of the position: ticket.py flatten --symbol "
                          f"{done.get('symbol')}")
    state.setdefault("orders", []).append({
        "at": at.isoformat(timespec="seconds"), "date": today, "index": a.index,
        "fingerprint": fingerprint(order), "symbol": str(order.get("symbol", "")).upper(),
        "side": order.get("side"), "plain": plain(order), "status": "declined",
        "reply": a.reply or "no"})
    write_json(a.state, state)
    print(f"Recorded: you said no to order {a.index} ({plain(order)}). Nothing was sent.")
    return 0


def cmd_result(a) -> int:
    orders, conf, risk, state, today, at = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError(f"order {a.index} has no yes recorded today; a result can only follow "
                          f"an approval")
    new = {"at": at.isoformat(timespec="seconds"), "date": today, "index": a.index,
           "fingerprint": fp, "symbol": rec["symbol"], "side": rec["side"], "plain": rec["plain"],
           "status": a.status, "paper": is_paper(rec)}
    for key in ("quantity", "kind", "max_loss", "stop", "product", "order"):
        if rec.get(key) is not None:
            new[key] = rec[key]
    if "order" not in new:
        new["order"] = order_core(order)
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    filled = a.status == "placed" and a.filled_qty and a.filled_price
    if (a.filled_qty or a.filled_price) and not filled:
        raise TicketError("a fill needs both --filled-qty and --filled-price, on a placed order")
    # The fill first: if it cannot be recorded, nothing is, rather than a placed order in the log
    # with no position behind it.
    if filled:
        check_fill_size(state, new, a.filled_qty, today)
        pf = apply_fill(a.portfolio, conf, rec["symbol"], rec["side"], a.filled_qty,
                        a.filled_price, today)
    state["orders"].append(new)
    write_json(a.state, state)
    print(f"Recorded order {a.index} as {a.status}{' (paper)' if new['paper'] else ''}.")
    if filled:
        log_fill(a.state, at, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, fp)
        print(f"Added the fill to {a.portfolio}.")
        if pf.get("_started_from"):
            print(f"Started {a.portfolio} from {pf['_started_from']}.")
        if rec.get("kind") == "entry":
            print_protection(new, a.filled_qty, a.filled_price)
    elif a.status == "placed":
        if rec.get("kind") == "entry" and protection_call(order):
            print(f"NO STOP EXISTS YET. Zerodha holds no stop for this order until the fill is "
                  f"recorded and the stop placed. When it fills (all or part), record it: "
                  f"ticket.py fill --symbol {rec['symbol']} --side {rec['side']} --qty Q --price P; "
                  f"it prints the stop for those shares.")
        else:
            print(f"When it fills, record it: ticket.py fill --symbol {rec['symbol']} --side "
                  f"{rec['side']} --qty Q --price P")
    return 0


def cmd_fill(a) -> int:
    conf = _config(a)
    at = _now(a.now)
    day = real_date(a.date, "--date") if a.date else at.date().isoformat()
    if not a.symbol.strip():
        raise TicketError("--symbol wants the stock, like --symbol SBIN")
    a.symbol = a.symbol.strip()
    # A fill on the same side as today's placed entry for this stock is more of that entry: it is
    # measured against what is left of the order, and it gets its own protective stop.
    state = load_state(a.state)
    entry = placed_entry(state, a.symbol, day, a.side)
    if entry:
        check_fill_size(state, entry, a.qty, day)
    pf = apply_fill(a.portfolio, conf, a.symbol, a.side, a.qty, a.price, day)
    log_fill(a.state, at, day, a.symbol, a.side, a.qty, a.price,
             entry.get("fingerprint") if entry else None)
    print(f"Recorded {a.side} {_num(a.qty)} {a.symbol.upper()} at {_rs(a.price)} on {day} in "
          f"{a.portfolio}. Cash now {_rs(pf['cash'])}.")
    if pf.get("_started_from"):
        print(f"Started {a.portfolio} from {pf['_started_from']}.")
    if entry:
        print_protection(entry, a.qty, a.price)
    elif a.side == "buy":
        print(f"No placed {a.symbol.upper()} entry today in {a.state}, so no protective stop is "
              f"printed for these shares.")
    else:
        bought = placed_entry(state, a.symbol, day, "buy")
        held = next((_row_shares(p) for p in pf.get("positions", [])
                     if _row_symbol(p) == a.symbol.upper()), 0)
        if bought and protection_call(bought.get("order") or {}) and held > 0:
            # A sale that was not the stop leaves the stop at Zerodha for more than is held now:
            # if it then triggered it would sell shares you no longer have.
            print(f"{_num(held)} {a.symbol.upper()} still held. If this sale was not the stop "
                  f"itself, the stop at Zerodha is still for the shares it was placed for: "
                  f"change it to {_num(held)} (modify_order, or modify_gtt_order for a GTT) or "
                  f"cancel it, so it cannot sell more than you hold. Ask the user first: that "
                  f"change is its own yes and its own Allow once card.")
    return 0


def cmd_flatten(a) -> int:
    conf = _config(a)
    symbol = a.symbol.strip().upper()
    if not symbol:
        raise TicketError("--symbol wants the stock to close, like --symbol SBIN")
    at, today = _clock(a)
    source = "Zerodha, get_positions"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 41")
    pf = None
    if a.qty is None:
        pf = read_portfolio(a.portfolio) or {}
        qty = sum(_held(p, a.portfolio) for p in pf.get("positions") or []
                  if _row_symbol(p) == symbol)
        source = str(a.portfolio)
        if qty <= 0:
            raise TicketError(f"nothing to flatten: no {symbol} shares in {a.portfolio}. Check "
                              f"Zerodha (get_positions) and pass --qty if you do hold some")
    # The product has to be the one the position was opened with. An MIS sell of a CNC holding is
    # a new intraday short, not a sale of the holding.
    state = load_state(a.state)
    entry = placed_entry(state, symbol, today)
    recorded = (entry or {}).get("product") or ((entry or {}).get("order") or {}).get("product")
    recorded = _up(recorded, "") or None
    if recorded and a.product and a.product != recorded:
        raise TicketError(f"--product {a.product} disagrees with today's placed {symbol} entry, "
                          f"which was {recorded}. Selling with the wrong product does not close "
                          f"the position; check get_positions and drop --product or fix it")
    earlier = None
    if not recorded and not a.product:
        # No entry today: a delivery entry placed on an earlier day is a holding, and sold as MIS
        # it would be a new intraday short. So is any position whose lots were all bought before
        # today, whatever the log says: an MIS position cannot be carried overnight.
        earlier = last_delivery_entry(state, symbol, today)
        if earlier is None:
            if pf is None:
                try:
                    pf = read_portfolio(a.portfolio) or {}
                except TicketError:
                    pf = {}
            days = [str(lot.get("date") or "") for p in pf.get("positions") or []
                    if isinstance(p, dict) and _row_symbol(p) == symbol
                    for lot in p.get("lots") or [] if isinstance(lot, dict)]
            if days and all(d and d < today for d in days):
                raise TicketError(f"the {symbol} shares in {a.portfolio} were bought before today "
                                  f"({', '.join(sorted(set(days)))}), so they are a delivery "
                                  f"holding, not an intraday position, and an MIS sell of them "
                                  f"would be a new intraday short. Check get_holdings / "
                                  f"get_positions and pass --product CNC (or MTF)")
    product = recorded or a.product or (earlier or {}).get("product") or "MIS"
    if entry and product == "MIS":
        # Only what today's MIS entry bought is an intraday position. Shares held from before (a
        # delivery holding in the same portfolio) sold as MIS would be a new intraday short.
        bought = filled_for(state, entry.get("fingerprint"), today)
        if not bought and a.qty is None:
            # No fill of today's entry is recorded, so none of the shares in the portfolio came
            # from it: they were held from before, and an MIS sell of them would be a short.
            raise TicketError(f"today's MIS {symbol} entry has no fill recorded, so the shares in "
                              f"{a.portfolio} are not from it. Record its fill first (ticket.py "
                              f"fill --symbol {symbol} --side buy --qty Q --price P), or pass "
                              f"--qty with the MIS quantity get_positions shows")
        if bought and qty > bought:
            raise TicketError(f"{_num(qty)} {symbol} is more than today's MIS entry filled "
                              f"({bought}). An MIS sell of the rest would be a new intraday short, "
                              f"not a sale of shares held from before: flatten --qty {bought} for "
                              f"the intraday part, and sell any delivery shares separately with "
                              f"--product CNC")
    product_from = (f"today's placed {symbol} entry in {a.state}" if recorded else
                    "--product" if a.product else
                    f"the placed {symbol} delivery entry of {rec_day(earlier)} in {a.state}"
                    if earlier else
                    f"the MIS default: no placed {symbol} entry is recorded today, so check "
                    f"get_positions and pass --product CNC if it is a delivery holding")
    # The plan only names the flat-by time. A damaged one must not stop the exit, so the config's
    # time is used and the ticket says why.
    try:
        plan = read_object(a.plan, "plan", required=False) or {}
        plan_note = ""
    except TicketError as err:
        plan, plan_note = {}, f" ({err}; flat-by taken from the config instead)"
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == symbol else None) \
        or conf.get("flat_by") or "15:20"
    order = {"symbol": symbol, "exchange": "NSE", "side": "sell", "quantity": int(qty),
             "order_type": "MARKET", "price": None, "product": product, "validity": "DAY",
             "why": (f"flat by {flat_by} IST: closes the {symbol} position before the close "
                     f"(quantity from {source})")}
    # Getting out must never be stopped by a damaged proposal file: it is replaced, and said so.
    try:
        old = read_json(a.orders, "orders", required=False)
        damaged = False
    except TicketError:
        old, damaged = None, True
    replaced = (len(old.get("orders") or []) if isinstance(old, dict)
                and isinstance(old.get("orders"), list) else 0)
    write_json(a.orders, {
        "created_by": "nse-broker", "created_at": at.isoformat(timespec="seconds"),
        "status": "proposed", "paper": is_paper(conf),
        "note": ("Flatten ticket. Nothing is placed until /nse-risk passes it and you say yes in "
                 "the chat."), "source": f"ticket.py flatten {symbol} on {today}",
        "orders": [order]})
    print(f"Flatten {symbol}: {plain(order)} (flat by {flat_by} IST){plan_note}.")
    print(f"Product {product}, from {product_from}.")
    print(f"Wrote it to {a.orders}" + (f" (it replaced {replaced} earlier proposal"
                                        f"{'' if replaced == 1 else 's'})" if replaced else
                                        " (it replaced a damaged file)" if damaged else "")
          + ".")
    if a.qty is None:
        print(f"The quantity comes from {source}. Check it against get_positions before you say "
              f"yes.")
    print("Next:")
    print("  1. /nse-risk: risk.py. A sell that only closes what you hold is never blocked by the")
    print("     daily limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    print("  3. If the protective SL-M stop is still open at Zerodha, cancel it first, or the sale and")
    print("     the stop together sell twice: get_orders shows it (status TRIGGER PENDING), then")
    print("     cancel_order with variety regular and its order_id (its own Allow once). A GTT is")
    print("     removed with delete_gtt_order. Then place_order with the ticket.")
    if product == "MIS":
        print("  MIS positions still open near 15:20 are squared off by Zerodha itself, with a "
              "charge.")
    if is_paper(conf):
        print("  PAPER: nothing was ever sent, so there is no stop at Zerodha to cancel; record the "
              "sale with ticket.py result after the yes.")
    return 0


def cmd_log(a) -> int:
    state = load_state(a.state)
    _, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print(f"No decisions recorded{'' if a.all else ' today'}.")
        return 1
    for r in rows:
        extra = f" id {r['broker_order_id']}" if r.get("broker_order_id") else ""
        reply = (f' reply "{r["reply"]}"' if r.get("reply") and r["status"] in
                 ("approved", "declined") else "")
        loss = (f" max loss {_rs(r['max_loss'])}" if r.get("max_loss") is not None
                and r["status"] == "placed" else "")
        paper = " (paper)" if r.get("paper") and r["status"] == "placed" else ""
        index = f"#{r['index']}" if r.get("index") is not None else "  "
        print(f"{r.get('at')}  {index} {r.get('status'):<9} {r.get('plain')}{reply}{loss}"
              f"{paper}{extra}")
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print(f"{placed} placed{'' if a.all else ' today'}.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    def clocked(p):
        p.add_argument("--now", help="pin the clock, like '2026-10-22 10:20' (IST)")
        p.add_argument("--today", help="YYYY-MM-DD (default: today, IST)")

    def common(p):
        p.add_argument("--orders", type=Path, default=show.shared("orders.json"))
        p.add_argument("--config", help="your settings file")
        p.add_argument("--risk", type=Path, default=show.shared("state") / "risk.json")
        p.add_argument("--state", type=Path, default=show.shared("state") / "broker.json")
        clocked(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--portfolio", type=Path, default=show.shared("portfolio.json"))
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id", help="Zerodha's order_id from the place_order response")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", type=Path, default=show.shared("portfolio.json"))
    p = sub.add_parser("fill", help="record a fill in nse/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date", help="YYYY-MM-DD (default: today, IST)")
    p.add_argument("--now")
    p.add_argument("--portfolio", type=Path, default=show.shared("portfolio.json"))
    p.add_argument("--config")
    p.add_argument("--state", type=Path, default=show.shared("state") / "broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a position by flat-by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from get_positions (default: nse/portfolio.json)")
    p.add_argument("--product", choices=["MIS", "CNC", "MTF"], type=str.upper,
                   help="the position's product (default: today's placed entry, else MIS)")
    p.add_argument("--state", type=Path, default=show.shared("state") / "broker.json",
                   help="the broker log, where today's placed entry gives the product")
    p.add_argument("--orders", type=Path, default=show.shared("orders.json"),
                   help="where the ticket is written")
    p.add_argument("--portfolio", type=Path, default=show.shared("portfolio.json"))
    p.add_argument("--plan", type=Path, default=show.shared("plan.json"))
    p.add_argument("--config")
    clocked(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", type=Path, default=show.shared("state") / "broker.json")
    clocked(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline,
                "result": cmd_result, "fill": cmd_fill, "flatten": cmd_flatten,
                "log": cmd_log}[a.cmd](a)
    except TicketError as err:
        print(f"ticket.py: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    show.run(main)
