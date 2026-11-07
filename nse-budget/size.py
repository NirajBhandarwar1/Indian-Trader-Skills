#!/usr/bin/env python3
"""Budget Manager for NSE stocks. Turns a plan into an exact number of shares.

Reads the plan from /nse-exit, finds the whole shares that fit inside four limits (max loss per
trade, position cap, max order, cash after the buffer), says WHICH limit decided it, and writes one
bracket order to nse/orders.json.

The stop decides the size: the further away the stop, the fewer shares. A stop too far away for the
max loss is a plain refusal, not a smaller order.

Nothing is placed. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import clock, show                                 # noqa: E402
from nsetools import settings as rules_file                      # noqa: E402

# Indian equities have NO fractional shares at all -- not at any broker, for any product. A US tool
# allows fractions outside a bracket order; here whole shares are the only thing that exists, so
# every limit floors.
FRESH_MINUTES = 15

DEFAULTS = {
    # No default capital. The US pack refuses to size with no budget, and so does this: a share
    # count worked from a figure the user never gave is a number that looks like theirs and is not.
    "budget": None,
    "cash_buffer_pct": 10.0,
    "risk_per_trade_percent": 1.0,
    "max_position_pct": 75.0,
    "max_order_rupees": 40000.0,
    "min_order_rupees": 500.0,
    "min_reward_risk": 1.0,
    "blocked": [],
    "paper": True,
    # The US pack's contract always carries a flat-by time (its config example's 15:55 New York).
    # This pack's config example sets 15:20 IST, the usual broker square-off for MIS.
    "flat_by": "15:20",
}
NO_BUDGET = ("no capital to size against: pass --budget, or set capital (or budget) in "
             "config.json")
# The setup names a plan carries, in the words a person reads, as the US pack writes them.
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
DISCLAIMER = "Education and tooling, not financial advice."


class Refused(Exception):
    """No order, with the reason. A sizing refusal carries the limits it was worked out from."""

    def __init__(self, message, limits=None, why_price=None, context=None):
        super().__init__(message)
        self.limits, self.why_price, self.context = limits, why_price, context


CONFIG_PATH: str | None = None     # set from --config
PORTFOLIO_PATH: str | None = None  # set from --portfolio
PRICES_PATH: str | None = None     # set from --prices
EPS = 1e-9
# No real account, order or share price is anywhere near these. Past them floating point stops
# counting whole paise (a budget of 1e308 crashed the share count with an OverflowError, and in
# the US pack too), so a figure that large is refused as not real rather than sized.
MOST = 1e13            # rupees: ten lakh crore
MOST_PRICE = 1e7       # rupees a share
MOST_WEIGHT = 1e9


def read_json(path, what: str):
    """A file's JSON, or a refusal that says which file and why. Never a traceback."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Refused(f"no {what} at {path}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise Refused(f"{path} is not valid JSON: {err}") from None
    except OSError as err:
        raise Refused(f"cannot read {path}: {err}") from None


def _finite(value) -> float | None:
    """A finite number from a number or a numeric string ("958.6"), else None.

    nan and inf are None: every comparison against nan is False, so a nan price passes "above 0"
    and "stop below entry" checks unseen. A JSON true is not a number either.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _setting(settings: dict, key: str) -> float:
    """A config value that has to be a number, or a refusal that names it."""
    number = _finite(settings.get(key))
    if number is None:
        raise Refused(f"{key} in your config has to be a number, not {settings.get(key)!r}")
    if abs(number) > MOST:
        raise Refused(f"{key} in your config is {number:g}, which is not a real figure "
                      f"(more than Rs {MOST:,.0f})")
    return number


def _optional(settings: dict, key: str) -> float | None:
    """A limit the US pack skips when it is not set: None for a missing, null, empty or 0 value,
    the number otherwise, and a refusal for anything that is not a number ("abc", nan)."""
    raw = settings.get(key)
    if raw is None or raw == "":
        return None
    number = _setting(settings, key)
    return number or None


def blocked_set(settings: dict) -> set[str]:
    """The blocked symbols in capitals. A list, or one string of names split on commas.

    A string was walked letter by letter, so "SBIN" blocked S, B, I and N and never SBIN; a
    number in the list crashed the split. Neither may let a blocked name through unseen.
    """
    raw = settings.get("blocked") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple, set)):
        raise Refused(f"blocked in your config has to be a list of symbols, not {raw!r}")
    return {str(s).strip().upper() for s in raw if str(s).strip()}


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def setup_words(setup) -> str:
    return SETUP_WORDS.get(setup, setup) if setup else ""


def _paise(value: float) -> int:
    """A price in whole paise, half up, as the US pack works in whole cents."""
    return int(math.floor(float(value) * 100 + 0.5 + EPS))


def rs(value: float) -> str:
    """Rs 1,000 for a whole figure, Rs 89,999.50 when the paise matter: a rounded figure must
    never be printed next to a share count that was worked out from the unrounded one."""
    value = float(value)
    if abs(value - round(value)) < 0.005:
        return f"Rs {round(value):,.0f}"
    return f"Rs {value:,.2f}"


def _exact(price: float) -> str:
    """A price as given, not rounded for display: 961.837 must not be printed as 961.84."""
    if abs(price * 100 - round(price * 100)) < 1e-6:
        return f"{price:,.2f}"
    return f"{price:,.6f}".rstrip("0")


def off_tick(name: str, price: float) -> str | None:
    """Why a price is not one NSE accepts, naming the nearest valid ticks; None if it is fine.

    The price is never moved for the user. Rounding a limit up pays more than they said; rounding
    a stop either way changes the risk the size was worked from.
    """
    tick = clock.tick_size(price)
    steps = price / tick
    if abs(steps - round(steps)) < 1e-6:
        return None
    down, up = clock.to_tick(price, up=False), clock.to_tick(price, up=True)
    return (f"the {name} Rs {_exact(price)} is not on an NSE tick (Rs {tick:.2f} at this "
            f"price): the nearest valid prices are Rs {down:,.2f} and Rs {up:,.2f}")


def config() -> dict:
    """Your rules over the defaults, from --config or the usual place. See nsetools.settings."""
    written = rules_file.load(CONFIG_PATH)
    settings = dict(DEFAULTS)
    settings.update(written)
    # capital is what this pack's risk skill already calls it; budget is what this one calls it.
    # Either is accepted so a user who set one does not have to set the other.
    #
    # Decided on what the FILE says, not on the merged result. The first version checked whether
    # budget was missing after the defaults had been merged in, so it was never missing, and a
    # user who set only capital -- which is what config.example.json does -- was sized against
    # the default 1 lakh instead of their own figure, with nothing to say so.
    # Kept as written and checked as a number where it is used, so a capital of "abc" is a plain
    # refusal rather than a traceback here.
    # A budget left empty (null or "") is not set, so capital is used if that is.
    if written.get("budget") in (None, "") and "capital" in written:
        settings["budget"] = written["capital"]
        settings["_budget_key"] = "capital"        # so a refusal names the key the user wrote
    return settings


def budget_of(settings: dict) -> float:
    """The capital to size against, or a refusal. Never a default: the US pack refuses with no
    budget ("no budget: pass --budget or set budget"), and so does this."""
    raw = settings.get("budget")
    if raw is None or raw == "":
        raise Refused(NO_BUDGET)
    number = _finite(raw)
    if number is None:
        raise Refused(f"{settings.get('_budget_key') or 'budget'} in your config has to be a "
                      f"number, not {raw!r}")
    if number <= 0:
        raise Refused(NO_BUDGET)
    if number > MOST:
        raise Refused(f"a capital of {number:g} is not a real figure (more than Rs {MOST:,.0f})")
    return number


def max_loss(settings: dict) -> float:
    """The rupees one trade may lose if the stop fills at the stop.

    An absolute limit wins if it is set, otherwise a percentage of the budget. Both are offered
    because a beginner thinks in percentages and anyone sizing a real account thinks in rupees.
    """
    raw = settings.get("max_loss_per_trade_rupees")
    if raw is not None and raw != "":
        absolute = _finite(raw)
        if absolute is None or absolute < 0 or absolute > MOST:
            raise Refused(f"max_loss_per_trade_rupees in your config has to be a number of "
                          f"rupees, 0 to use the percentage, not {raw!r}")
        if absolute:
            return absolute
    percent = _setting(settings, "risk_per_trade_percent")
    if percent < 0:
        raise Refused(f"risk_per_trade_percent in your config cannot be below 0, not "
                      f"{percent:g}")
    return budget_of(settings) * percent / 100.0


def quote_time(saved: dict, symbol: str) -> datetime | None:
    """When THIS symbol's price was quoted: its own detail.at, or None.

    Per symbol, never the file's. The prices file is merged by /nse-alerts, so quoting ITC now
    rewrites the file's time while SBIN's price in it may be from yesterday's close. Judging every
    price by the file's time made a day-old SBIN price look fresh. Nor does when it was fetched
    stand in for when it was quoted: a price with no quote time of its own is unverified, and an
    unverified price never sizes a trade.
    """
    details = (saved or {}).get("detail") if isinstance(saved, dict) else None
    if not isinstance(details, dict):
        return None
    detail = next((v for k, v in details.items() if str(k).strip().upper() == symbol), None)
    raw = detail.get("at") if isinstance(detail, dict) else None
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=clock.IST)
    return when.astimezone(clock.IST)


def shares_for(rupees: float, per_share: float) -> int:
    """Whole shares that fit, worked in whole paise so floating point cannot lose a share."""
    have, each = int(round(float(rupees) * 100)), int(round(float(per_share) * 100))
    if have <= 0 or each <= 0:
        return 0
    return have // each


def entry_price(plan: dict, limit: float | None, at: datetime) -> tuple[float, str]:
    """The price to size at, and why that one.

    A fresh quote ABOVE the plan's entry means a bigger risk at the same stop, so fewer shares. A
    lower one never shrinks the risk: the plan's entry stands. That asymmetry is deliberate -- a
    price that ran away from you is a worse trade, not a cheaper one.
    """
    if limit is not None:
        return float(limit), f"your limit price Rs {float(limit):,.2f}"
    planned = _paise(float(plan["entry"])) / 100.0
    symbol = str(plan["symbol"]).strip().upper()
    plain = f"the plan's entry Rs {planned:,.2f}"
    path = Path(PRICES_PATH) if PRICES_PATH else show.shared("prices.json")
    if not path.exists():
        # A file named with --prices that is not there is refused, as the US pack refuses it:
        # the user believes a live price was checked, and none could be.
        if PRICES_PATH:
            raise Refused(f"no prices at {path}: fix --prices, or leave it out to size at the "
                          f"plan's entry")
        return planned, plain
    saved = read_json(path, "prices")
    table = saved.get("prices") if isinstance(saved, dict) else None
    if not isinstance(table, dict):
        return planned, (f"{plain} ({path.name} has no table of prices in it, so no live price "
                         f"was checked)")
    raw = next((v for k, v in table.items() if str(k).strip().upper() == symbol), None)
    if raw is None:
        return planned, f"{plain} (no {symbol} price in {path.name})"
    quoted = _finite(raw)
    # Each reason a saved price is not used is said, as the US pack says it, never skipped silently.
    if quoted is None or not 0 < quoted <= MOST_PRICE:
        return planned, (f"{plain} (the {symbol} price {raw!r} in {path.name} is not a usable "
                         f"price, so it was not used)")
    quoted = _paise(quoted) / 100.0
    seen = quote_time(saved, symbol)
    if seen is None:
        return planned, (f"{plain} (the Rs {quoted:,.2f} in {path.name} has no quote time of its "
                         f"own, so it is unverified and was not used)")
    # A minute of clock difference either way is allowed, as the US pack allows it.
    if not timedelta(seconds=-60) <= at - seen <= timedelta(minutes=FRESH_MINUTES):
        return planned, (f"{plain} (the Rs {quoted:,.2f} in {path.name} is from "
                         f"{seen:%Y-%m-%d %H:%M} IST, not the last {FRESH_MINUTES} minutes, so it "
                         f"was not used)")
    if _paise(quoted) > _paise(planned):
        return quoted, (f"the live price Rs {quoted:,.2f} (quoted {seen:%H:%M} IST), above the "
                        f"plan's Rs {planned:,.2f}, so the risk a share is bigger and the size "
                        f"smaller")
    return planned, (f"{plain} (the live Rs {quoted:,.2f}, quoted {seen:%H:%M} IST, is not above "
                     f"it, and a lower price never shrinks the risk)")


def size(plan: dict, settings: dict | None = None, limit: float | None = None,
         at: datetime | None = None) -> dict:
    """The shares, the limit that decided them, and the order. Or a refusal."""
    settings = settings or config()
    at = at or clock.now()
    if not isinstance(plan, dict):
        raise Refused("the plan is not a JSON object (run /nse-exit)")
    symbol = str(plan.get("symbol") or "").strip().upper()
    if not symbol:
        raise Refused("the plan has no symbol (run /nse-exit)")

    # "BUY" is a buy, as the US pack lower-cases the side before it looks.
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise Refused(f"this sizes long (buy) trades only; the plan says {plan.get('side')!r}")
    # Today is today in India, whatever zone the clock was handed in.
    today = at.astimezone(clock.IST).date() if at.tzinfo else at.date()
    if plan.get("date") and str(plan["date"]) != today.isoformat():
        raise Refused(f"the plan is for {plan['date']} and today is {today}. Make a new one "
                      f"with /nse-exit")

    values = {}
    for key in ("entry", "stop", "target"):
        value = _finite(plan.get(key))
        if value is None or value <= 0:
            raise Refused(f"the plan needs a positive {key} (run /nse-exit): an order with its "
                          f"exits needs the entry, the stop and the target")
        if value > MOST_PRICE:
            raise Refused(f"the plan's {key} Rs {value:g} is not a real share price (run /nse-exit)")
        values[key] = value
    if symbol in blocked_set(settings):
        raise Refused(f"{symbol} is on your blocked list")
    # The budget and the buffer are checked where the US pack checks them: after the plan and
    # the blocked list, before any price is looked at.
    budget = budget_of(settings)
    buffer_pct = _optional(settings, "cash_buffer_pct") or 0.0
    if not 0 <= buffer_pct < 100:
        raise Refused("cash_buffer_pct must be between 0 and 100")
    if limit is not None:
        limit = _finite(limit)
        if limit is None or limit <= 0 or limit > MOST_PRICE:
            raise Refused("a limit price has to be a number above 0, and a real share price")
    # The exits and a limit go to the exchange as they are, so each has to be a price it takes.
    for name, price in (("limit price", limit), ("stop", values["stop"]),
                        ("target", values["target"])):
        if price is not None and off_tick(name, price):
            raise Refused(off_tick(name, price) + (". Give --limit one of them" if name ==
                                                   "limit price" else
                                                   ". Make a new plan with /nse-exit"))

    entry, why_price = entry_price(plan, limit, at)
    planned = _paise(values["entry"]) / 100.0
    entry_from = ("limit" if limit is not None else
                  "price" if _paise(entry) != _paise(planned) else "plan")
    stop, target = values["stop"], values["target"]
    # Worked in whole paise, as the US pack works in whole cents. In floating point 72.65 - 67.65
    # over 67.65 - 62.65 is 0.9999999999999986, and a trade of exactly 1R was refused as under 1.
    entry_p, stop_p, target_p = _paise(entry), _paise(stop), _paise(target)
    if stop_p >= entry_p:
        raise Refused(f"the stop Rs {stop:,.2f} is not below the entry Rs {entry:,.2f}")
    if target_p <= entry_p:
        raise Refused(f"the target Rs {target:,.2f} is not above the entry Rs {entry:,.2f}: the "
                      f"move it was waiting for has already happened")
    risk_p, reward_p = entry_p - stop_p, target_p - entry_p
    risk = risk_p / 100.0
    reward_risk = reward_p / risk_p
    # Not set (missing, null or 0) means no floor, as the US pack skips it.
    floor = _optional(settings, "min_reward_risk") or 0.0

    # A position cap or a max order that is not set (missing, null or 0) is not a limit, as the
    # US pack leaves it out. The cash limit is always there, so the size is still bounded.
    cap_pct = _optional(settings, "max_position_pct")
    max_order = _optional(settings, "max_order_rupees")
    for key, number in (("max_position_pct", cap_pct), ("max_order_rupees", max_order)):
        if number is not None and number < 0:
            raise Refused(f"{key} in your config cannot be below 0 (0 means no such limit), "
                          f"not {number:g}")
    least = _optional(settings, "min_order_rupees") or 0.0
    most_lost = max_loss(settings)

    buffer = round(budget * buffer_pct / 100.0, 2)
    cash = budget - buffer
    cash_from = f"{rs(budget)} capital less the {rs(buffer)} cash buffer"
    held = Path(PORTFOLIO_PATH) if PORTFOLIO_PATH else show.shared("portfolio.json")
    if held.exists():
        account = read_json(held, "portfolio")
        in_hand = _finite(account.get("cash")) if isinstance(account, dict) else None
        # A portfolio whose cash cannot be read is refused, as /nse-risk refuses it. Sizing
        # past it would size money the account may not have.
        if in_hand is None:
            raise Refused(f"{held} has no cash figure that is a number "
                          f"({account.get('cash') if isinstance(account, dict) else account!r}): "
                          f"fix it, or point --portfolio at the right file")
        # The buffer stays in cash whichever figure is smaller. The first version took the
        # portfolio's cash as it was, so the buffer was never kept back from it.
        if in_hand - buffer < cash - 0.005:
            cash = in_hand - buffer
            cash_from = f"{rs(in_hand)} cash in the portfolio less the {rs(buffer)} cash buffer"
    cash = max(round(cash, 2), 0.0)

    rupees, maths = {"max loss": (most_lost, risk)}, {
        "max loss": f"{rs(most_lost)} max loss / Rs {risk:,.2f} risk a share"}
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        rupees["position cap"] = (cap, entry)
        maths["position cap"] = f"{cap_pct:g}% of {rs(budget)} = {rs(cap)} / Rs {entry:,.2f}"
    if max_order:
        rupees["max order"] = (max_order, entry)
        maths["max order"] = f"{rs(max_order)} / Rs {entry:,.2f}"
    rupees["cash"] = (cash, entry)
    maths["cash"] = f"{cash_from} = {rs(cash)} / Rs {entry:,.2f}"
    shares = {name: shares_for(*rupees[name]) for name in rupees}
    quantity = min(shares.values())
    # The first limit in the US pack's order that allows the fewest: max loss, position cap, max
    # order, cash. Breaking a tie alphabetically named "cash" where max loss was the reason.
    decided = next(name for name in rupees if shares[name] == quantity)

    table = {name: {"shares": shares[name], "math": maths[name], "decides": name == decided}
             for name in rupees}
    # The plan's words are text wherever they are printed: an entry_time of 5 crashed the report.
    setup, entry_time = _text(plan.get("setup")), _text(plan.get("entry_time"))
    flat_by = _text(plan.get("flat_by")) or _text(settings.get("flat_by"))
    # What the US pack reports about every sizing, a refused one included.
    context = {"symbol": symbol, "side": "buy", "setup": setup, "date": plan.get("date"),
               "entry_time": entry_time, "entry_plan": planned, "entry": round(entry, 2),
               "entry_from": entry_from, "why_price": why_price, "stop": stop, "target": target,
               "risk_per_share": round(risk, 2), "reward_risk": round(reward_risk, 2),
               "stop_label": _text(plan.get("stop_label")),
               "target_label": _text(plan.get("target_label")),
               "budget": budget, "cash_buffer_pct": buffer_pct, "cash_buffer": buffer,
               "cash_available": cash, "max_loss_per_trade_rupees": round(most_lost, 2),
               "flat_by": flat_by, "order_type": "LIMIT" if limit is not None else "MARKET",
               "limits": table, "decided_by": decided, "quantity": quantity}
    if quantity < 1:
        if decided == "max loss":
            why = (f"one share risks Rs {risk:,.2f} (entry Rs {entry:,.2f} minus stop "
                   f"Rs {stop:,.2f}), more than your {rs(most_lost)} max loss per "
                   f"trade. Skip this trade, or plan a closer stop with /nse-exit")
        else:
            why = (f"one share of {symbol} costs Rs {entry:,.2f}, more than your {decided} limit "
                   f"({maths[decided]})")
        raise Refused(why, table, why_price, context)
    if reward_p < floor * risk_p - EPS:
        raise Refused(f"reward:risk {reward_risk:.2f} (target Rs {target:,.2f}, entry "
                      f"Rs {entry:,.2f}, stop Rs {stop:,.2f}) is under your minimum {floor:g}, so "
                      f"/nse-risk would block it. Skip it, or make a new plan with /nse-exit",
                      table, why_price, context)
    value = round(quantity * entry_p / 100.0, 2)
    if value < least - 0.005:
        raise Refused(f"{quantity} share(s) of {symbol} is Rs {value:,.2f}, under your minimum "
                      f"order of {rs(least)}", table, why_price, context)

    loss = round(quantity * risk_p / 100.0, 2)
    bound = {"max loss": f"{rs(most_lost)} max loss per trade", "position cap": "position cap",
             "max order": "max order", "cash": "cash after the buffer"}[decided]
    order = {
        "symbol": symbol, "exchange": "NSE", "side": "buy", "quantity": quantity,
        "order_type": "LIMIT" if limit is not None else "MARKET",
        "price": round(entry, 2) if limit is not None else None,
        "product": "MIS", "validity": "DAY", "order_class": "bracket",
        "stop_loss_price": stop, "take_profit_price": target,
        "why": (f"{setup_words(setup) or 'day trade'} on {symbol}: {quantity} "
                f"shares, bound by your {bound} ({quantity} x Rs {risk:,.2f} = {rs(loss)} at "
                f"the stop)"),
        "plan": {"entry": round(entry, 2), "stop": stop, "target": target,
                 "risk_per_share": round(risk, 2), "max_loss": loss,
                 "setup": setup, "entry_time": entry_time,
                 "flat_by": flat_by or None, "date": plan.get("date")},
        "paper": bool(settings.get("paper", True)),
    }
    return dict(context, value=value, max_loss=loss, order=order, notes=[])


def _allocate(rows: list[dict], investable: float, cap: float) -> tuple[dict, set]:
    """Exact rupees per symbol with the cap enforced.

    Proportional to weight; anything over the cap is pinned at it and the rest of the money is
    shared over the others, repeated until nothing is over.
    """
    alloc, capped = {}, set()
    active, money_left = list(rows), investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["symbol"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["symbol"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["symbol"]] = cap
            capped.add(r["symbol"])
            money_left -= cap
        active = [r for r in active if r["symbol"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["symbol"]] = 0.0
            break
    return alloc, capped


def _round_to_unit(exact: dict, capped: set, order: list[str], cap: float,
                   unit: float) -> dict:
    """Round each slice to the unit and keep the total.

    The total placed is the exact total rounded DOWN, so it never spends money you do not have.
    Each slice is rounded to the nearest unit, then the difference goes to the biggest name not
    capped, then the next, never pushing anyone over the cap.
    """
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {t: cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
           for t, v in exact.items()}
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff and takers and guard < 100000:
        moved = False
        for t in takers:
            if not diff:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def _whole_shares(rupees: dict, prices: dict, order: list[str], cap: float,
                  cash: float) -> dict:
    """Floor each slice to whole shares, then buy one more share at a time for the name furthest
    below its slice, while the cash lasts and the cap holds. Nothing is left idle by accident."""
    shares = {t: int(math.floor(rupees[t] / prices[t] + EPS)) if rupees[t] > 0 else 0
              for t in order}
    left = cash - sum(shares[t] * prices[t] for t in order)
    while True:
        best, gap = None, None
        for t in order:
            price = prices[t]
            if price > left + EPS or (shares[t] + 1) * price > cap + EPS:
                continue
            g = rupees[t] - shares[t] * price
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def split(weights: dict[str, float], settings: dict,
          prices: dict[str, float] | None = None, top: int | None = None,
          paise: bool = False, names: dict[str, str] | None = None) -> dict:
    """Divide the capital across a list by weight. The US pack's algorithm, in rupees.

    A different job from sizing one trade: there is no stop, so nothing decides the size except
    the weights and the limits.

      1. Blocked symbols and non-positive weights come out; then only the --top N biggest stay.
      2. The cash buffer comes off the top. The position cap is a share of the WHOLE budget.
      3. Each name gets money in proportion to its weight; anything over the cap is pinned at it
         and its excess shared out, repeatedly.
      4. Slices are rounded keeping the total, never spending money that is not there.
      5. With prices, whole shares: floor each, then add one share at a time to the name furthest
         below its slice while the cash lasts. Indian equities have no fractional shares at all.
      6. A slice under the minimum order -- or worth less than one share -- is dropped, smallest
         weight first, and the money RE-SPLIT among the rest, so nothing sits idle by accident.

    Without prices the split is in rupees, for reading only: a rupee amount is not an Indian
    order, so it cannot be written as one. With prices, every name needs one, and a missing price
    is a refusal that names it rather than a slice that quietly becomes no order.

    THESE ARE PLAIN BUYS WITH NO STOP. /nse-risk refuses them while it requires one.
    """
    budget = budget_of(settings)
    buf_pct = _setting(settings, "cash_buffer_pct")
    cap_pct = _setting(settings, "max_position_pct")
    least = _setting(settings, "min_order_rupees") if settings.get("min_order_rupees") else 0.0
    if top is not None and top < 1:
        raise Refused("--top wants how many names to keep: 1 or more")
    if not 0 <= buf_pct < 100:
        raise Refused("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise Refused("max_position_pct must be above 0 and at most 100")

    blocked = blocked_set(settings)
    removed = sorted(s for s in weights if s.upper() in blocked)
    keep = [{"symbol": s, "source_weight": float(w)} for s, w in weights.items()
            if s.upper() not in blocked and float(w) > 0]
    dropped = [f"{s} (weight is zero or negative)" for s, w in weights.items()
               if s.upper() not in blocked and float(w) <= 0]
    keep.sort(key=lambda r: -r["source_weight"])
    if top:
        dropped += [f"{r['symbol']} (outside the top {top})" for r in keep[top:]]
        keep = keep[:top]
    if not keep:
        raise Refused("nothing to split: no weights are left once blocked names, zero and "
                      "negative weights" + (f" and names outside the top {top}" if top else "")
                      + " are taken out")

    whole = prices is not None
    if whole:
        prices = {str(k).strip().upper(): v for k, v in prices.items()}
        missing = [r["symbol"] for r in keep if prices.get(r["symbol"]) is None]
        if missing:
            raise Refused(f"whole shares need a price for: {', '.join(missing)}. Add them with "
                          f"/nse-alerts quote {' '.join(missing)} --write")
        # A negative price looped forever buying shares that cost less than nothing, and a nan
        # price crashed the floor. Neither is a price; the prices file is fixed, not guessed round.
        bad = [f"{r['symbol']} {prices[r['symbol']]!r}" for r in keep
               if not 0 < (_finite(prices[r["symbol"]]) or 0) <= MOST_PRICE]
        if bad:
            raise Refused(f"a price has to be a number above 0, and these are not: "
                          f"{', '.join(bad)}. Quote them again with /nse-alerts")
        # A price NSE does not trade at is not a quote (Rs 0.001 sized one share at a time for
        # seconds); it is refused, as plan mode refuses an off-tick limit or stop.
        for r in keep:
            why = off_tick(f"{r['symbol']} price", _finite(prices[r["symbol"]]))
            if why:
                raise Refused(why.replace("nearest valid prices", "nearest prices NSE trades "
                                          "at") + ". Quote it again with /nse-alerts")
        prices = {r["symbol"]: _finite(prices[r["symbol"]]) for r in keep}

    cash_buffer = round(budget * buf_pct / 100, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100, 2)
    # Whole rupees by default, as the US pack splits in whole dollars; --paise splits to the paisa,
    # as its --cents does. With prices the split is whole shares, which needs the paisa anyway.
    unit = 0.01 if (whole or paise) else 1.0

    let_go = []      # dropped by the loop below, so a refusal can say each real reason
    while True:
        live = [dict(r) for r in keep]
        total = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / total
        exact, capped = _allocate(live, investable, cap)
        order = [r["symbol"] for r in sorted(live, key=lambda r: -exact[r["symbol"]])]
        rupees = _round_to_unit(exact, capped, order, cap, unit)
        slices = dict(rupees)
        shares = None
        if whole:
            shares = _whole_shares(rupees, prices, order, cap, sum(rupees.values()))
            rupees = {t: round(shares[t] * prices[t], 2) for t in order}
        # The US pack's rule exactly: only a slice under the minimum order is dropped and its
        # money re-split. With no minimum, a name whose slice will not buy one share stays at 0
        # shares (said below, never hidden); dropping it too re-split the money and gave other
        # share counts than the US pack's.
        small = [r for r in live if rupees[r["symbol"]] < least - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["symbol"]))
        sym = worst["symbol"]
        if whole and shares[sym] == 0:
            why = (f"{sym} (one share costs {rs(prices[sym])}, more than its slice of "
                   f"{rs(slices[sym])})")
        else:
            why = (f"{sym} (its slice of {rs(rupees[sym])} is under your {rs(least)} minimum "
                   f"order)")
        dropped.append(why)
        let_go.append(why)
        keep = [r for r in keep if r["symbol"] != sym]
        if not keep:
            # Named for what happened. "Under your minimum order" was said with no minimum set,
            # when the reason was that one share cost more than the whole slice.
            raise Refused(f"nothing is left to buy: {'; '.join(let_go)}. Raise the budget or "
                          f"hold fewer stocks")

    rows, unbought = [], []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["symbol"]
        row = {"symbol": t, "name": (names or {}).get(t) or "",
               "weight_percent": round(r["weight"], 2), "source_weight": r["source_weight"],
               "rupees": rupees[t], "capped": t in capped}
        if whole:
            row["price"] = float(prices[t])
            row["shares"] = shares[t]
            if not shares[t]:
                unbought.append(f"{t} (one share costs {rs(prices[t])}, more than its slice of "
                                f"{rs(slices[t])}; no order)")
        rows.append(row)
    invested = round(sum(row["rupees"] for row in rows), 2)
    return {"budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
            "budget_after_buffer": investable, "min_order_rupees": least,
            "cap": cap, "max_position_pct": cap_pct, "slices": rows, "invested": invested,
            "idle": round(investable - invested, 2), "dropped": dropped,
            "unbought": unbought, "capped": sorted(capped), "whole_shares": whole,
            "blocked": removed}


def weight_orders(out: dict, source: str = "") -> list[dict]:
    """Weights mode as orders: one plain delivery buy per slice, in whole shares.

    The US pack can send a dollar amount; Indian equities take a share count only, so a split
    without prices has no quantity and cannot become an order at all. These carry no stop, and the
    gate blocks them while require_stop is on -- a long-term split belongs under its own config.
    """
    orders = []
    for row in out["slices"]:
        if not row.get("shares"):
            continue
        why = f"{row['weight_percent']:.2f}% of the split"
        if source:
            why += f", weights from {source}"
        if row.get("capped"):
            why += (f"; capped at {out['max_position_pct']:g}% of the {rs(out['budget'])} "
                    f"budget")
        if row.get("price") is not None:
            why += f"; {row['shares']} x Rs {row['price']:,.2f} = {rs(row['rupees'])}"
            if row.get("price_at"):
                why += f" (price {row['price_at']})"
        orders.append({"symbol": row["symbol"], "exchange": "NSE", "side": "buy",
                       "quantity": int(row["shares"]), "order_type": "MARKET", "price": None,
                       "product": "CNC", "validity": "DAY", "why": why,
                       "plan": {"entry": row.get("price")}})
    return orders


def orders_file(orders: list[dict], settings: dict, source: str = "",
                at: datetime | None = None) -> dict:
    """The file /nse-risk reads: the orders, and a header saying what they are and are not.

    Stamped with the pinned clock when there is one, as the US pack stamps it, so a replayed day
    writes a file dated that day.
    """
    from datetime import timezone
    when = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {"created_by": "nse-budget",
            "created_at": when.replace(microsecond=0).isoformat(),
            "status": "proposed", "paper": bool(settings.get("paper", True)),
            "note": ("Proposals only. Nothing is placed until /nse-risk passes each order and you "
                     "decide on it yourself."),
            "source": source, "orders": orders}


def weights_from(rows: list[tuple], where: str) -> dict[str, float]:
    """(symbol, weight) pairs -> {SYMBOL: weight}, or a refusal naming the bad one.

    One check for --weights and --targets alike: a weight that is not a finite number, a row with
    no symbol, and a symbol given twice are each refused, as the US pack refuses them.
    """
    weights: dict[str, float] = {}
    for name, value in rows:
        symbol = str(name or "").strip().upper()
        if not symbol:
            raise Refused(f"a weight in {where} has no symbol")
        if symbol in weights:
            raise Refused(f"{symbol} is listed twice in the weights")
        number = _finite(value.strip() if isinstance(value, str) else value)
        if number is not None and abs(number) > MOST_WEIGHT:
            raise Refused(f"the weight for {symbol} is {number:g}: weights are shares of 100, "
                          f"and no real one is that large")
        if number is None:
            shown = value.strip() if isinstance(value, str) else value
            raise Refused(f"the weight for {symbol} is not a number: {shown!r}")
        weights[symbol] = number
    return weights


def targets_from(raw, where: str) -> list[tuple]:
    """A weights file -> (symbol, weight) pairs.

    Both shapes the US pack reads: {"weights": {"SBIN": 40}} and {"weights": [{"symbol": "SBIN",
    "weight": 40}]} ("ticker" for "symbol", "weight_pct" or "pct" for "weight", as there). The list
    form is the one its own example file uses, and it crashed here.
    """
    data = raw.get("weights", raw) if isinstance(raw, dict) else raw
    if isinstance(data, dict):
        return [(k, v) for k, v in data.items() if not str(k).startswith("_")]
    if isinstance(data, list):
        rows = []
        for i, row in enumerate(data):
            if not isinstance(row, dict):
                raise Refused(f"weights[{i}] in {where} is not an object")
            name = row.get("symbol") or row.get("ticker")
            value = row.get("weight", row.get("weight_pct", row.get("pct")))
            if not name or value is None:
                raise Refused(f"weights[{i}] in {where} needs a symbol and a weight")
            rows.append((name, value))
        return rows
    raise Refused(f"the weights in {where} must be a list or an object")


def prices_from(path) -> dict:
    """A prices file's {SYMBOL: price}, keys in capitals; each price is checked where it is used."""
    saved = read_json(path, "prices")
    table = (saved.get("prices") or saved) if isinstance(saved, dict) else None
    if not isinstance(table, dict):
        raise Refused(f"{path} has no prices in it")
    return {str(k).strip().upper(): v for k, v in table.items() if not str(k).startswith("_")}


def names_in(raw) -> dict[str, str]:
    """{SYMBOL: company name} from the list form of a weights file, as the US pack shows them."""
    data = raw.get("weights", raw) if isinstance(raw, dict) else raw
    if not isinstance(data, list):
        return {}
    out = {}
    for row in data:
        if isinstance(row, dict) and row.get("name"):
            symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
            if symbol:
                out[symbol] = str(row["name"])
    return out


def price_ages(path, symbols, at: datetime) -> dict[str, str]:
    """{SYMBOL: how old its price is}, said for every price a split uses.

    The US pack splits at whatever the prices file holds, however old. Here each price is shown
    with its own quote time, and one that is not from the last 15 minutes -- or has no time at
    all -- is called that, because a share count worked from yesterday's close is not one worked
    from a live price.
    """
    saved = read_json(path, "prices")
    out = {}
    for symbol in symbols:
        seen = quote_time(saved, symbol)
        if seen is None:
            out[symbol] = "undated: no quote time of its own, so unverified"
        elif timedelta(seconds=-60) <= at - seen <= timedelta(minutes=FRESH_MINUTES):
            out[symbol] = f"quoted {seen:%Y-%m-%d %H:%M} IST"
        else:
            out[symbol] = (f"quoted {seen:%Y-%m-%d %H:%M} IST, not the last {FRESH_MINUTES} "
                           f"minutes")
    return out


def report(sized: dict) -> None:
    """The plan, the four limits, the size and the order it proposes, as the US pack prints them."""
    order = sized["order"]
    for note in sized.get("notes") or []:
        print(note)
    header(sized)
    print(f"\n  {sized['quantity']} shares of {sized['symbol']} = Rs {sized['value']:,.2f} at "
          f"Rs {sized['entry']:,.2f}, decided by the {sized['decided_by']} limit.")
    print(f"  Max loss at the stop: {sized['quantity']} x Rs {sized['risk_per_share']:,.2f} "
          f"= Rs {sized['max_loss']:,.2f}.")
    how = (f"LIMIT Rs {order['price']:,.2f}" if order["order_type"] == "LIMIT" else "MARKET")
    print(f"\n  Proposed order (nothing has been placed): buy {order['quantity']} "
          f"{order['symbol']}, {how}, {order['product']}, day, take-profit "
          f"Rs {order['take_profit_price']:,.2f}, stop-loss Rs {order['stop_loss_price']:,.2f}")
    if sized["flat_by"]:
        print(f"  Flat by {sized['flat_by']} IST: anything still open then gets closed "
              f"(/nse-alerts rings, /nse-broker flatten).")
    print(f"\n  A stop is an order, not a promise: in a fast market or on a circuit it can "
          f"fill\n  below Rs {sized['stop']:,.2f}, so the real loss can be larger. Nothing was "
          f"placed.")
    print(f"  Next: /nse-risk checks it against your day limits, then /nse-broker asks you yes "
          f"or no.")
    print(f"  {DISCLAIMER}")


def header(sized: dict) -> None:
    """The plan, the price it is sized at, and every limit: printed for a size and for a refusal
    alike, as the US pack prints its whole report either way."""
    print(f"{sized['symbol']} buy, {setup_words(sized['setup']) or 'setup'}, entry "
          f"Rs {sized['entry_plan']:,.2f}"
          f"{' at ' + sized['entry_time'] if sized['entry_time'] else ''}")
    print(f"Stop Rs {sized['stop']:,.2f}{': ' + sized['stop_label'] if sized['stop_label'] else ''}."
          f" Target Rs {sized['target']:,.2f}"
          f"{': ' + sized['target_label'] if sized['target_label'] else ''}.")
    print(f"Sized at {sized['why_price']}. Risk Rs {sized['risk_per_share']:,.2f} a share (entry "
          f"Rs {sized['entry']:,.2f} minus stop Rs {sized['stop']:,.2f}). Reward:risk "
          f"{sized['reward_risk']:.2f}.\n")
    print("Limits (whole shares; the smallest one wins):")
    limits_table(sized["limits"])


def limits_table(limits: dict) -> None:
    """Every limit, its math in full and which one decides. The math column is as wide as the
    longest line: a fixed width cut the cash row to "...cash ..." and hid its figure."""
    width = max([len(body["math"]) for body in limits.values()] + [8])
    show.table([{"limit": name, "math": body["math"], "shares": body["shares"],
                 "decides": "<- decides" if body.get("decides") else ""}
                for name, body in limits.items()],
               [("limit", "limit", 14), ("math", "the math", width), ("shares", "shares", 7),
                ("decides", "", 10)])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", nargs="?", const=True, default=None,
                    help="a limit order at the plan's entry, or at the price you give")
    ap.add_argument("--now", help="pin the clock, like '2026-10-22 10:20' (IST)")
    ap.add_argument("--weights", help="split the capital, like SBIN=40,ITC=60")
    ap.add_argument("--targets", help="a JSON file of weights instead of --weights")
    ap.add_argument("--prices", help="prices to size at (default nse/prices.json if it exists)")
    ap.add_argument("--top", type=int, help="weights mode: keep only the N biggest weights")
    ap.add_argument("--paise", "--cents", dest="paise", action="store_true",
                    help="weights mode: split to the paisa instead of whole rupees")
    ap.add_argument("--whole-shares", action="store_true",
                    help="weights mode: whole shares (Indian equities have no fractions, so this "
                         "is what prices give; with no prices at all it is refused)")
    ap.add_argument("--budget", type=show.number, help="override the capital in the config")
    ap.add_argument("--portfolio", help="your account; its cash caps the size (default "
                                        "nse/portfolio.json)")
    ap.add_argument("--write", nargs="?", const=True, metavar="PATH",
                    help="write the orders, to nse/orders.json or to PATH")
    ap.add_argument("--plan", help="the plan to size (default nse/plan.json)")
    ap.add_argument("--config", help="a config file instead of the usual places")
    ap.add_argument("--out", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    global CONFIG_PATH, PORTFOLIO_PATH, PRICES_PATH
    CONFIG_PATH, PORTFOLIO_PATH, PRICES_PATH = args.config, args.portfolio, args.prices

    def refused(message: str) -> int:
        # With --json a refusal is JSON too, so a caller parsing the output is never handed text.
        message = message.rstrip(".")
        print(json.dumps({"refused": message}, indent=2) if args.json else f"Refused: {message}.")
        return 2

    try:
        settings = config()
    except FileNotFoundError as err:
        return refused(f"{err}. A config named with --config has to exist")
    except rules_file.BadConfig as err:
        return refused(str(err))
    if args.budget is not None:
        if args.budget <= 0:
            return refused("--budget must be more than zero")
        if args.budget > MOST:
            return refused(f"--budget {args.budget:g} is not a real figure (more than "
                           f"Rs {MOST:,.0f})")
        settings["budget"] = float(args.budget)
    where = args.out or (args.write if isinstance(args.write, str) else None)
    writing = bool(args.write or args.out)
    try:
        clock.now(args.now)
    except clock.BadTime as err:
        return refused(str(err))

    # An empty --weights "" is a split with nothing in it, not a request to size the plan.
    splitting = args.weights is not None or args.targets is not None
    if not splitting and (args.top or args.paise or args.whole_shares):
        return refused("--top, --whole-shares and --paise work with --weights or --targets")
    if args.top is not None and args.top < 1:
        return refused("--top wants how many names to keep: 1 or more")
    try:
        if splitting:
            return split_main(args, settings, writing, where)
        return plan_main(args, settings, writing, where)
    except Refused as err:
        return refused(str(err))


def write_orders(doc: dict, where) -> Path:
    """Write the orders file whole or not at all (show.hand_off writes beside it, then moves it
    over), so /nse-risk can never read half a file; a write that fails is a refusal."""
    try:
        return show.hand_off("orders.json", doc, where)
    except OSError as err:
        raise Refused(f"could not write the orders to {where or 'nse/orders.json'}: {err}. "
                      f"Nothing was written") from None


def split_main(args, settings: dict, writing: bool, where) -> int:
    at = clock.now(args.now)
    names: dict[str, str] = {}
    if args.targets:
        raw = read_json(args.targets, "weights file")
        weights = weights_from(targets_from(raw, args.targets), args.targets)
        names = names_in(raw)
        source = (raw.get("source") if isinstance(raw, dict) else None) or args.targets
    else:
        rows = []
        for piece in args.weights.split(","):
            piece = piece.strip()
            if not piece:
                continue
            # A name with no weight is refused, as the US pack refuses it: guessing a weight
            # of 1 would put a sliver of money where the user meant something else.
            name, sep, value = piece.partition("=")
            if not sep or not name.strip():
                raise Refused(f"--weights wants SYMBOL=WEIGHT pairs, got {piece!r}")
            rows.append((name, value))
        weights = weights_from(rows, "--weights")
        source = f"--weights {args.weights}"
    # The saved prices are used when they are there, as plan mode uses them and as the US pack
    # uses its own prices file in a split.
    path = Path(args.prices) if args.prices else show.shared("prices.json")
    prices = prices_from(path) if (args.prices or path.exists()) else None
    if args.whole_shares and prices is None:
        raise Refused("--whole-shares needs a price for every name: add --prices "
                      "nse/prices.json (write it with /nse-alerts quote SYM ... --write)")
    out = split(weights, settings, prices, args.top, args.paise, names)
    stale = []
    if out["whole_shares"]:
        ages = price_ages(path, [row["symbol"] for row in out["slices"]], at)
        for row in out["slices"]:
            row["price_at"] = ages[row["symbol"]]
            if not ages[row["symbol"]].endswith("IST"):
                stale.append(row["symbol"])
    orders = weight_orders(out, source)
    notes = []
    if stale:
        notes.append(f"Not live prices: {', '.join(stale)} (see each price's time below). The "
                     f"share counts are worked from them, and a market order fills at the "
                     f"price then, not at these. Quote them again with /nse-alerts quote "
                     f"{' '.join(stale)} --write.")
    written = None
    if writing:
        # A split in rupees is for reading only: an Indian order needs a share count. A split in
        # whole shares is written as the US pack writes it, even when no slice buys a share.
        if not out["whole_shares"]:
            raise Refused("nothing written: Indian equities take a share count, so a split "
                          "needs --prices to become orders")
        written = write_orders(orders_file(orders, settings, source, at), where)
    if args.json:
        print(json.dumps(dict(out, orders=orders, notes=notes,
                              wrote=str(written) if written else None),
                         indent=2, default=str))
        return 0
    for note in notes:
        print(note)
    if args.targets:
        print(f"Weights from: {source}")
    print(f"Budget {rs(out['budget'])}, cash buffer {out['cash_buffer_pct']:g}% = "
          f"{rs(out['cash_buffer'])}, investing {rs(out['budget_after_buffer'])}, cap "
          f"{out['max_position_pct']:g}% = {rs(out['cap'])} per stock, "
          + (f"in whole shares at the prices in {path}" if out["whole_shares"] else "by rupees")
          + ".\n")
    has_names = any(row["name"] for row in out["slices"])
    shown = [dict(row, rupees=rs(row["rupees"]), weight_percent=f"{row['weight_percent']:.2f}%",
                  price=(f"Rs {row['price']:,.2f}" if row.get("price") is not None else None),
                  capped=(f"capped at {out['max_position_pct']:g}%" if row["capped"] else ""))
             for row in out["slices"]]
    columns = [("symbol", "symbol", 12)] + ([("name", "name", 22)] if has_names else []) + [
        ("weight_percent", "weight", 8), ("rupees", "rupees", 14)]
    if out["whole_shares"]:
        columns += [("price", "price", 12), ("shares", "shares", 7),
                    ("price_at", "price time", max(len(r["price_at"]) for r in out["slices"]))]
    columns += [("capped", "note", 16)]
    show.table(shown, columns)
    print(f"\n  invested     {rs(out['invested'])}")
    print(f"  cash buffer  {rs(out['cash_buffer'])}")
    print(f"  left idle    {rs(out['idle'])}")
    if out["idle"] >= 1:
        print(f"\n  Left idle {rs(out['idle'])}: " + (
            "whole shares do not divide evenly" if out["whole_shares"]
            else "every stock is at its cap; add stocks or raise max_position_pct") + ".")
    for line in out["dropped"]:
        print(f"\n  dropped {line}")
    for line in out["unbought"]:
        print(f"\n  0 shares of {line}")
    if out["blocked"]:
        print(f"\n  left out as blocked: {', '.join(out['blocked'])}; their share went to "
              f"the rest.")
    if out["capped"]:
        print(f"\n  pinned at the {out['max_position_pct']:g}% cap: "
              f"{', '.join(out['capped'])}, and the excess shared out.")
    if orders:
        print("\n  Proposed orders (nothing has been placed):")
        for order in orders:
            print(f"    buy {order['quantity']} {order['symbol']}, MARKET, CNC, day")
    elif out["whole_shares"]:
        print("\n  No orders: no slice buys one whole share at these prices.")
    else:
        print("\n  No orders: Indian equities take a share count, so a split in rupees is for "
              "reading only.\n  Give --prices to turn it into orders.")
    if written:
        print(f"\n  wrote {len(orders)} order(s) to {written}")
    print("\n  THESE HAVE NO STOP. /nse-risk refuses a trade without one while require_stop "
          "is on,\n  and counts each against the day's trade limit. Nothing was placed.")
    print("  Next: /nse-risk checks every order, then /nse-broker asks you yes or no for each "
          "one.")
    print(f"  {DISCLAIMER}")
    return 0


def plan_main(args, settings: dict, writing: bool, where) -> int:
    path = Path(args.plan) if args.plan else show.shared("plan.json")
    if not path.exists():
        raise Refused(f"no {path}. Run /nse-exit first")
    plan = read_json(path, "plan")

    at = clock.now(args.now)
    limit = None
    if args.limit is True or (isinstance(args.limit, str) and args.limit.strip().lower() == "plan"):
        # At the plan's entry, as the US pack's --limit and --limit plan both are. A plan with no
        # usable entry is refused by size() for that reason.
        limit = _finite(plan.get("entry")) if isinstance(plan, dict) else None
    elif args.limit is not None:
        limit = _finite(args.limit)
        if limit is None or not limit > 0:
            raise Refused("--limit wants a price, like --limit 961.85")

    try:
        sized = size(plan, settings, limit, at)
    except Refused as err:
        if args.json:
            print(json.dumps({"refused": str(err), "limits": err.limits,
                              "sized_at": err.why_price, "sizing": err.context, "orders": [],
                              "notes": []},
                             indent=2, default=str))
            return 2
        if err.context:
            header(err.context)
            print()
        print(f"Refused, and no order written: {str(err).rstrip('.')}.")
        print(DISCLAIMER)
        return 2

    written = None
    if writing:
        source = (f"{path} ({sized['symbol']} {sized['setup'] or 'plan'} "
                  f"{plan.get('date') or at.date()})")
        written = write_orders(orders_file([sized["order"]], settings, source, at), where)
    if args.json:
        print(json.dumps(dict(sized, orders=[sized["order"]],
                              wrote=str(written) if written else None), indent=2, default=str))
    else:
        report(sized)
        if written:
            print(f"\nWrote 1 proposed bracket order to {written}")
    return 0


if __name__ == "__main__":
    show.run(main)
