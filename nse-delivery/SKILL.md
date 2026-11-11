---
name: nse-delivery
description: >-
  Delivery Screen. Separates real buying from intraday churn on NSE stocks using the
  delivery percentage the exchange publishes and nothing outside India does. Finds
  delivery spikes against a stock's own norm, quiet accumulation, and one stock's
  delivery history. Use when asked whether a move is genuine, to screen for
  accumulation, or to check who is actually keeping the shares.
argument-hint: "[spikes | high PCT | accumulation | symbol SYM] [--days N] [--json]"
---

# Delivery Screen

Every exchange publishes volume. NSE also publishes how much of that volume was **taken delivery
of** — shares that moved into someone's demat account instead of being bought and sold again before
the close. This skill reads that one number and nothing else. It reports what was published; it does
not predict anything and it is not financial advice.

A stock up 8% on huge volume with 12% delivery was traded, not bought. The same move on 70%
delivery means somebody took the shares home.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python "${CLAUDE_SKILL_DIR}/delivery.py" --spikes              # delivery vs each stock's own 20-day median
python "${CLAUDE_SKILL_DIR}/delivery.py" --high 60             # companies closing above 60% delivery
python "${CLAUDE_SKILL_DIR}/delivery.py" --accumulation        # price flat, delivery climbing
python "${CLAUDE_SKILL_DIR}/delivery.py" --symbol RELIANCE     # one stock, day by day
```

`--days N` sets the comparison window (default 20), `--top N` the row count (default 25), `--json`
prints machine-readable output for another skill to read. Exit code 0 on success, 1 if no mode was
given, 2 if the exchange has not published yet.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/delivery.json` | written by `--spikes` and `--accumulation` with `--json` | `nse-scan` ranks candidates, `nse-risk` sizes them |
| nothing | read | this skill needs no other skill to run first |

It needs no token and no account. The exchange's daily bhavcopy is public.

## The universe

1. Start from the day's bhavcopy, about 2,706 symbols.
2. **Drop the funds.** Keep only symbols in the exchange's equity master list, 2,359 of them. An
   exchange traded fund is held rather than traded, so it sits near 100% delivery permanently.
   Without this filter the whole first page is LIQUIDBETF, BANKIETF and MOMENTUM30 and not one
   company — measured 2026-10-23.
3. **Drop the untradeable.** Require at least 100 lakh of turnover on the day. A stock that traded
   four hundred shares shows 100% delivery because the one buyer kept them; that is arithmetic, not
   accumulation.
4. Require a delivery figure and a close. Both are missing for suspended scrips.

## Reading the result

| Delivery | Usually means |
|---|---|
| under 20% | intraday churn; the move often does not hold |
| 20-40% | ordinary for a liquid large cap |
| 40-70% | real participation |
| over 70% | accumulation, or a stock that barely trades |

Conventions, not laws. Use them to sort what to read about.

`--spikes` compares a stock against **its own** median, because 60% means something different for
a stock that usually does 20% than for one that usually does 65%. It needs at least ten past days
before it will call anything unusual.

## Data and limits

- **Published once a day, after the close**, around 7pm India time. Before that, asking for today
  finds nothing, which the script reports as "not published yet" rather than as zero.
- **One file per day**, so a 20-day window costs 20 requests. Requests are paced to 2 a second.
- **A spike can be a single block deal.** Cross-check with `/nse-insider SYMBOL`, which names the
  buyer and seller for any deal above the disclosure threshold. A 60% day that is one institution
  buying is a different fact from the same day spread over a thousand accounts.
- Weekends and holidays have no file. The script walks back to the last day the exchange published.

## Steps

1. Run `--spikes` to see where delivery is unusual today.
2. For anything interesting, run `--symbol SYM` to see whether it is a one-day event or a trend.
3. Check `/nse-insider SYM` for a block deal or insider filing that explains it.
4. Check `/nse-news SYM` for an announcement on the same date.
5. Check `/nse-results SYM` before concluding anything about value.
6. Hand the symbol to `/nse-risk SYM` for sizing. This skill never decides what to buy.

## Example

```
$ python delivery.py --high 80 --top 4

Stocks closing above 80% delivery, busiest first

symbol        delivery%   close       price%    turnover
------------  ----------  ----------  --------  -----------
PREMEXPLN     84.79       687.4       -0.74     4491.4
SKFINDUS      84.72       2343.1      -4.72     3284.7
KIRLFER       82.6        452.15      0.02      1510.2
AUGMONT       85.97       1096.3      -4.99     909.0
```

## Rules

- Report the published figure. Never interpolate a missing day or average across a gap.
- Compare a stock against itself, never against the market.
- Say how many days of history a conclusion rests on.
- Label a partial window as partial rather than reporting it as a full one.

## What it never does

- Never names a stock to buy or sell.
- Never places, prepares or sizes an order.
- Never treats high delivery as a signal on its own.
- Never fills in a day the exchange has not published.

## Safety

High delivery means somebody kept the shares. It does not mean they were right to. Nothing here is
financial advice, and no part of this skill can place a trade.
