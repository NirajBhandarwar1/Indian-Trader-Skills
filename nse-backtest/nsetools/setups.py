"""The rules a screen fires and a replay measures, defined once.

A rule screened on one definition and measured on another is a rule nobody has measured. So the
setups live here, and /nse-scan and /nse-backtest both import them, rather than one skill reaching
into the other's folder for them -- which also broke the moment either skill was installed alone.

A setup is a function that, given a stock's daily bars and a position in them, says whether to buy
at the NEXT open. Each reads only bars[..i] inclusive: today's bar is known at the close, tomorrow's
is not. A rule that looks at tomorrow has seen the answer and fakes an edge, and the tests poison
every later bar to prove none of these do.

Standard library only.
"""
from __future__ import annotations

import statistics

MIN_TURNOVER_LACS = 100.0         # below this a stock is not tradeable; see nse-delivery
COSTS_ROUND_TRIP_PERCENT = 0.293  # brokerage, STT both sides, exchange, GST, SEBI, stamp. See nse-risk.
RSI_PERIOD = 14
OVERSOLD = 30.0


def delivery_spike(bars: list[dict], i: int, window: int = 20) -> bool:
    """Delivery today is at least twice the stock's own median, on a rising close."""
    if i < window:
        return False
    past = [b["delivery_percent"] for b in bars[i - window:i]
            if b.get("delivery_percent") is not None]
    today = bars[i].get("delivery_percent")
    if today is None or len(past) < window // 2:
        return False
    normal = statistics.median(past)
    close, prev = bars[i].get("close"), bars[i].get("previous_close")
    if not normal or not close or not prev:
        return False
    return today >= normal * 2 and close > prev


def delivery_and_breakout(bars: list[dict], i: int, window: int = 20) -> bool:
    """Delivery above its median AND the close above the highest close of the window."""
    if i < window:
        return False
    past = bars[i - window:i]
    highs = [b["close"] for b in past if b.get("close")]
    deliv = [b["delivery_percent"] for b in past if b.get("delivery_percent") is not None]
    today, close = bars[i].get("delivery_percent"), bars[i].get("close")
    if not highs or not deliv or today is None or not close:
        return False
    return close > max(highs) and today > statistics.median(deliv)


def quiet_accumulation(bars: list[dict], i: int, window: int = 20) -> bool:
    """Delivery rising over the window while the price went nowhere."""
    if i < window:
        return False
    past = bars[i - window:i + 1]
    half = len(past) // 2
    early_d = [b["delivery_percent"] for b in past[:half] if b.get("delivery_percent") is not None]
    late_d = [b["delivery_percent"] for b in past[half:] if b.get("delivery_percent") is not None]
    early_c = [b["close"] for b in past[:half] if b.get("close")]
    late_c = [b["close"] for b in past[half:] if b.get("close")]
    if not (early_d and late_d and early_c and late_c):
        return False
    d0, d1 = statistics.median(early_d), statistics.median(late_d)
    c0, c1 = statistics.median(early_c), statistics.median(late_c)
    if d0 <= 0 or c0 <= 0:
        return False
    return (d1 / d0 - 1) > 0.25 and abs(c1 / c0 - 1) < 0.05


def plain_breakout(bars: list[dict], i: int, window: int = 20) -> bool:
    """The close above the window's highest close. No delivery involved: the control case.

    Here so the delivery setups can be compared against a rule that ignores delivery entirely. If
    they do not beat this, the delivery number is not adding anything.
    """
    if i < window:
        return False
    highs = [b["close"] for b in bars[i - window:i] if b.get("close")]
    close = bars[i].get("close")
    return bool(highs and close and close > max(highs))


def rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Wilder's RSI on closes, the same arithmetic the US pack uses.

    Needs period + 1 closes. Returns None with fewer, rather than an RSI of nothing: an RSI built on
    five days is a different number with the same name.
    """
    if len(closes) < period + 1:
        return None
    gains = [max(0.0, closes[k] - closes[k - 1]) for k in range(1, len(closes))]
    losses = [max(0.0, closes[k - 1] - closes[k]) for k in range(1, len(closes))]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    for k in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[k]) / period
        avg_loss = (avg_loss * (period - 1) + losses[k]) / period
    return 100 - 100 / (1 + avg_gain / avg_loss) if avg_loss else 100.0


def _closes_to(bars: list[dict], i: int) -> list[float]:
    return [b["close"] for b in bars[:i + 1] if b.get("close")]


def oversold(bars: list[dict], i: int, window: int = 20) -> bool:
    """RSI 14 under 30 today. A falling knife as often as a bargain, which is why it is measured."""
    value = rsi(_closes_to(bars, i))
    return value is not None and value < OVERSOLD


def oversold_bounce(bars: list[dict], i: int, window: int = 20) -> bool:
    """RSI 14 was under 30 yesterday, and today closed up. The knife at least stopped."""
    if i < 1:
        return False
    yesterday = rsi(_closes_to(bars, i - 1))
    close, prev = bars[i].get("close"), bars[i].get("previous_close")
    return (yesterday is not None and yesterday < OVERSOLD
            and bool(close) and bool(prev) and close > prev)


def bull_flag(bars: list[dict], i: int, window: int = 20) -> bool:
    """Up more than 10% over the ten sessions before the last five, then the last five closes
    inside a 5% range. The pole, then the flag. The US pack's definition, to the session."""
    if i < 15:
        return False
    c = _closes_to(bars, i)
    if len(c) < 16 or not c[-16]:
        return False
    run = c[-6] / c[-16] - 1
    last = c[-1]
    span = (max(c[-5:]) - min(c[-5:])) / last if last else 1.0
    return run > 0.10 and span < 0.05


def reclaim_ma20(bars: list[dict], i: int, window: int = 20) -> bool:
    """Closed under its 20-day average yesterday, closed above it today."""
    if i < 20:
        return False
    c = _closes_to(bars, i)
    if len(c) < 21:
        return False
    ma_today = sum(c[-20:]) / 20
    ma_yesterday = sum(c[-21:-1]) / 20
    return c[-2] < ma_yesterday and c[-1] > ma_today


SETUPS = {
    "delivery-spike": delivery_spike,
    "delivery-breakout": delivery_and_breakout,
    "accumulation": quiet_accumulation,
    "breakout": plain_breakout,
    "bull-flag": bull_flag,
    "reclaim-ma20": reclaim_ma20,
    "oversold": oversold,
    "oversold-bounce": oversold_bounce,
}

# The rules that need nothing but closes, which is all a batch quote gives. The delivery rules
# need the exchange's delivery figure, which only the cached bhavcopy carries.
CLOSE_ONLY = ("breakout", "bull-flag", "reclaim-ma20", "oversold", "oversold-bounce")
