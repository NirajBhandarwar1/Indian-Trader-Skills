---
name: nse-risk
description: >-
  Position Sizing. Takes an entry, a stop and your capital and answers PASS or BLOCK
  with the share count and the full Indian cost stack: brokerage, STT, exchange
  charges, GST, SEBI fee and stamp duty. Use when asked how many shares to buy, what
  a trade risks, whether reward justifies risk, or before any order is prepared.
argument-hint: "--entry PRICE --stop PRICE [--target PRICE] [--capital RS] [--risk PCT]"
---

# Position Sizing

The only skill here that can say **no**. Everything else reports what the exchange published; this
one does arithmetic on your money and refuses when the arithmetic does not work.

It needs no network and no account. The numbers come from you.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python ${CLAUDE_SKILL_DIR}/risk.py --entry 687.40 --stop 650 --target 780   # size it
python ${CLAUDE_SKILL_DIR}/risk.py --entry 687.40 --stop 650 --capital 500000
python ${CLAUDE_SKILL_DIR}/risk.py --entry 100 --stop 90 --open-positions 5 # against your limit
python ${CLAUDE_SKILL_DIR}/risk.py --entry 100 --stop 90 --json             # for another skill
```

`--risk PCT` overrides the risk per trade, `--config PATH` points at a different config file.

**Exit codes: 0 = PASS, 3 = BLOCK.** A caller can branch on that without parsing the text.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `config.json` | read | `capital`, `risk_per_trade_percent`, `max_positions` |
| nothing | written | the verdict goes to stdout; nothing is persisted |

Copy `config.example.json` to `config.json` and set your capital. Without it the tool assumes
₹1,00,000 and 1% risk, and says so.

## How the size is worked out

```
risk budget   = capital × risk_per_trade_percent
shares        = risk budget ÷ (entry − stop)
```

Then the size is cut if it exceeds your capital, and the full cost of getting in and out at the stop
is added to the loss. **Costs are not a footnote.** On a tight stop they are a material share of
what you are risking, which is why they are in the output rather than in a disclaimer.

The Indian cost stack, charged on a delivery trade: brokerage, STT on both sides, NSE transaction
charge, GST on brokerage and exchange charges, the SEBI fee, and stamp duty on the buy. Every rate
is rounded **up**. A sizing tool that flatters the arithmetic is worse than none, and a discount
broker will only make the real figure better than this.

## When it blocks

| It refuses when | Because |
|---|---|
| stop is at or above entry | this tool sizes long trades only |
| stop is closer than 1% | inside one minute's range on most Indian mid caps; it would be taken out by noise and the size it implies is unreal |
| stop is further than 25% | that is a different size of bet than this tool assumes |
| you already hold `max_positions` | your own limit, from config |
| one share risks more than the budget | the smallest possible position is already too big |

A block always says what would have to change. It never suggests a different stop, because choosing
a stop is a judgement about the chart and this skill does not look at charts.

## It warns without blocking when

- costs exceed a third of the risk budget — a wider stop or a bigger position would carry them better
- reward to risk is under 1.5 after costs — you would need to be right more often than not
- the position had to be cut to fit capital — the stop allowed more than you have

## Steps

1. Get a candidate from `/nse-delivery`, `/nse-scan` or `/nse-news`.
2. Decide an entry and a stop **yourself**, from the chart and the level.
3. Run this. If it says BLOCK, the trade does not get sized, full stop.
4. Only on PASS does anything downstream happen.

## Example

```
$ python risk.py --entry 687.40 --stop 650 --target 780 --capital 500000

PASS

   shares                133
   position              91,424.20  (18.3% of capital)
   stop                  650.00  (5.44% away)
   risk budget           5,000.00
   costs, round trip     261.55
   loss if stopped       5,235.75
   gain if target        12,030.15
   reward to risk        2.3
```

```
$ python risk.py --entry 687.40 --stop 685 --capital 500000

BLOCK

   - stop is 0.35% away, closer than 1.0%: inside one minute's range on most Indian
     mid caps, so it would be taken out by noise and the size it implies is unreal
```

## Rules

- Show the arithmetic, always. A number without its working is not usable.
- Round costs up, never down.
- A block is final within this skill. It does not offer a smaller stop to get to PASS.
- Say where capital and limits came from, so a wrong config is visible.

## What it never does

- Never chooses an entry, a stop or a target.
- Never suggests a stock.
- Never places or prepares an order.
- Never sizes a short, a future or an option. Long equity only.
- Never assumes leverage or margin.

## Safety

Sizing is arithmetic, not a recommendation. A PASS means the numbers are internally consistent with
your own stated risk; it says nothing about whether the trade is a good idea. Nothing here is
financial advice and no part of this skill can place a trade.
