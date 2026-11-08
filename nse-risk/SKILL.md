---
name: nse-risk
description: >-
  The Risk Gate. Checks every proposed order from /nse-budget against your rules,
  your account, fresh prices and what already went out today, and answers PASS or
  BLOCK for each, with the reason. Blocks a missing or wrong-side stop, a loss over
  your limit, a reward:risk under your floor, too much of the account in one stock,
  too little cash, a derivative, a short, a duplicate, a trade outside your hours, a
  day that has hit its loss or trade limit. Also sizes one idea from scratch with
  the full Indian cost stack. Getting out is never blocked by a day limit.
  Use when an order is about to be placed (before /nse-broker, always), or when asked
  "check my order", "is this safe", "can I take this trade", how much has been
  risked today, how many shares to buy, what a trade risks, or whether an order
  passes.
argument-hint: "[--orders FILE] [--now TIME] [--no-write] | --entry PRICE --stop PRICE"
---

# nse-risk

The only skill here that can say **no**. Everything else reports what the exchange published; this
one checks your order against your own rules and refuses when it breaks one.

It needs no network and no account: by default it uses the built-in NSE holiday list, and only
`--online` asks the exchange for it. Scripts live in `${CLAUDE_SKILL_DIR}`.

```bash
python "${CLAUDE_SKILL_DIR}/risk.py"                           # nse/orders.json; records the verdicts
python "${CLAUDE_SKILL_DIR}/risk.py" --now "2026-10-22 10:20"  # pin the clock (IST)
python "${CLAUDE_SKILL_DIR}/risk.py" --orders mine.json --json
python "${CLAUDE_SKILL_DIR}/risk.py" --no-write                # check without recording
python "${CLAUDE_SKILL_DIR}/risk.py" --entry 687.40 --stop 650 --target 780 --capital 500000
```

**Exit 0 when every order passes, 1 when any is blocked** (a `--config` that does not exist is a
BLOCK too, never a quiet fall back to the defaults). A file that exists but cannot be read --
orders, config, portfolio, prices, broker log or its own verdicts -- is refused with exit 1 and
says which; it is never read as empty. A `--now` it cannot read is exit 1 too. With no `capital`
in the config and no `nse/portfolio.json` to read the cash from, it refuses to check (exit 1)
rather than assume an account that is not yours. Flags: `--orders FILE` (default
`nse/orders.json`), `--config FILE`, `--portfolio FILE` (default `nse/portfolio.json`; without one, a
fresh account funded with your capital), `--prices FILE` (default `nse/prices.json`),
`--state FILE` (what went out today; default `nse/state/broker.json` if it exists), `--today DATE`
(the day the limits count from; the clock, and so the window and how fresh a price is, stays
`--now` or the real time), `--out FILE` (where verdicts go, default `nse/state/risk.json`),
`--no-write`, `--online` (fetch the holiday list from the exchange; without it the built-in list
is used, and a year it does not cover blocks entries and says so), `--json`. `--offline` is still
accepted and is the default. Sizing one idea: `--entry`,
`--stop`, `--target`, `--capital`, `--risk PCT`, `--open-positions N`.

## What it reads and writes

| File | Direction | What |
|---|---|---|
| `nse/orders.json` | read | the proposed orders, written by `nse-budget` (a header and a list, a bare list, or one order) |
| `config.json` | read | your rules, below |
| `nse/portfolio.json` | read | cash and positions; missing means a fresh account funded with your `capital` (and with no `capital` either, a refusal). Holdings are valued at a price from `nse/prices.json`, else at their cost, and it says so |
| `nse/prices.json` | read | fresh prices, written by `nse-alerts quote --write`; a price whose own quote time (`detail.at`) is not from the last 15 minutes, or that has none, is not used, and it says so. The file's `as_of` is when it was last written, not when each price was quoted, so it never makes a price fresh |
| `nse/state/broker.json` | read if present | what `/nse-broker` logged as placed today: duplicates, trades and orders per day, the daily loss |
| `nse/state/risk.json` | read and written | each verdict, with a fingerprint of the exact order; with no broker log, today's passes recorded here are what the day limits count |

## The checks, in order

Orders are checked in file order **as if each PASS had filled**, so a batch that is fine order by
order but breaks a limit together is caught.

| Check | Blocks when |
|---|---|
| format | no symbol, a side other than buy or sell, a fractional or zero quantity (Indian equities trade in whole shares), an unknown order type, validity, product or exchange, a LIMIT or SL with no price, an SL or SL-M with no trigger_price, a price, trigger, stop or target that is not a positive number |
| tick | a price, trigger, stop or target that is not a whole number of ticks (5 paise from Rs 250 up, 1 paisa below): the exchange would refuse it |
| asset | a derivative contract, monthly or weekly (NIFTY26OCT25000CE, NIFTY2610625000CE, RELIANCE26DECFUT), the NFO/BFO segment, or the NRML product, unless `allow_options` (and then a derivative must name NFO or BFO); a currency or crypto pair (BTCUSD, USDINR); anything that does not look like an NSE symbol |
| short | selling more than you hold, unless `allow_short` |
| product | (entries) MTF, margin trading funding, unless `allow_margin` |
| price | (entries) no price at all to size the order against |
| blocked | (entries) the symbol is on your `blocked` list |
| size | (entries) the order is under `min_order_rupees` or over `max_order_rupees` |
| position | (entries) the stock would be more than `max_position_pct` of the account (cash plus holdings) |
| positions | (entries) a stock not yet held while `max_positions` are already open |
| cash | (entries) a buy that would need more cash than you have (margin; `allow_margin` lifts only this), or would leave less than `cash_buffer_pct` of the account in cash (this applies even with `allow_margin`, as in the US pack) |
| stop | (entries) `require_stop` and there is none (the order's `stop_loss_price`, or its plan's stop), or it is on the wrong side; closer than `min_stop_percent` (inside one minute's range on most Indian mid caps); further than 25% |
| loss | (entries) shares x risk a share is over `max_loss_per_trade_rupees` when that is set above 0, otherwise over `risk_per_trade_percent` of capital |
| target, reward:risk | (entries) the target is on the wrong side, or reward:risk is under `min_reward_risk`, or there is no target to measure it against |
| daily loss | (entries) today's committed risk plus this order is over `max_daily_loss_rupees` when that is set above 0, otherwise over `max_daily_loss_percent` of capital |
| trades | (entries) today's entries plus this one exceed `max_trades_per_day` |
| window | (entries) before `no_entries_before` (09:30) or after `no_entries_after` (15:00) IST, outside the 09:15 to 15:30 session, or on a day the market is shut (a weekend, or an NSE holiday) |
| duplicate | (entries) a second order for the same stock and side in the batch, or one already sent today |
| daily limit | (entries) orders sent today plus this one exceed `max_orders_per_day` |

**An exit is not an entry.** An entry opens or adds to a position: a buy, or a sell that would go
short. An exit only reduces what you hold. Getting out must always be possible, so an exit is
checked for format, tick, asset and short sales only; every other rule (the day limits, the
window, loss, reward:risk, the blocked list, size, duplicates, MTF) turns into a warning for it,
never a BLOCK. A closing sell after the window still passes.

**The window is IST**, worked out in the tool (IST is UTC+5:30 all year), never from the
computer's clock zone. Weekends and NSE holidays are closed; set both `no_entries_*` to null to
switch the window off.

**The risk is measured from the fresh price.** If `nse/prices.json` has a price from the last 15
minutes above the plan's entry, the loss and reward:risk are worked from it (to the paisa, half
up), because that is what the order would pay. A LIMIT or SL order is measured from its own price,
an SL-M from its trigger.

**The same order checked twice is one trade.** Each verdict is recorded with a fingerprint of the
order's broker fields, so re-checking an order that passed today does not count it again -- not
against the order count, the trade count or the daily loss -- and a changed order is a new one.
Once `/nse-broker`'s log exists, it is what counts, as in the US pack: an order it shows as placed
today makes the same stock and side a duplicate. An older log record with no `kind` counts as an
entry only if it is a buy.

**Shares move as each PASS would fill**, priced or not: two sells of 10 from 15 held cannot both
be exits, so the second is a short.

Warnings do not block, but say them: a LIVE account, no `capital` in the config (the percent
limits are then worked from the portfolio's cash plus holdings, and it says the figure), a holding
with no price (valued at its cost), a stop that lives only in the plan and not at the broker, an
order's stop that differs from its plan's, an order class other than bracket, an entry in today's
log with no max loss (it is not counted in the daily loss), a price too old to use, and an order
with no plan entry whose only price is an old one (its figures are measured from that old price,
and it says so).

**Zerodha has no bracket order.** An order carrying a stop or a target passes with a warning
saying how `/nse-broker` places the exits: MIS entry, then an SL-M at the stop after each fill; the
target is never placed at Zerodha for MIS (watched by /nse-alerts); CNC/MTF get a GTT (two-leg, or
single-leg when stop only). Where an order gets none (a CNC or MTF sell, or an order with only a
target) the warning says so. This gate checks the exits but cannot attach them.

What is recorded is **risk committed, not loss realised**: without a broker connection nothing here
can know what a trade did, and recording an assumed loss would be inventing a number.

## Your rules (config.json)

`capital` none: set it (`config.example.json` has 1,00,000), or the gate works from
`nse/portfolio.json` and, with neither, refuses · `cash_buffer_pct` 10 · `max_position_pct` 75 · `max_order_rupees` 40,000 ·
`min_order_rupees` 500 · `risk_per_trade_percent` 1 · `max_loss_per_trade_rupees` 0 (off) ·
`max_daily_loss_percent` 3 · `max_daily_loss_rupees` 0 (off) · `max_trades_per_day` 3 ·
`max_orders_per_day` 10 · `max_positions` 5 · `min_reward_risk` 1.0 · `require_stop` true ·
`min_stop_percent` 1 · `no_entries_before` 09:30 · `no_entries_after` 15:00 · `blocked` [] ·
`allow_options`, `allow_margin`, `allow_short` false · `paper` true. A config with `budget` and no
`capital` is checked against the budget. A number written as text ("10") is read as that number;
text that is not a number, or a negative limit, is refused. A switch (`allow_*`, `require_stop`,
`paper`) must be true or false: "false" written as text is refused, never read as on; a null
`paper` is refused too, as `/nse-broker` refuses it (left out, it is paper). `blocked`
is a list of symbols ("SBIN" alone is read as ["SBIN"]). Times are IST `HH:MM`; set both
`no_entries_*` to null to switch the window off. `min_stop_percent` 0 switches the floor off, and
any limit set to 0 is off.

`min_stop_percent` is enforced **here only**. `/nse-exit` reads the same value and warns when a plan
is under it, so you find out when the plan is made, but it does not enforce it: two skills enforcing
one rule is how they come to disagree.

## Example: the real SBIN order

```
$ python risk.py --now "2026-10-22 10:20"

PASS  #1 buy SBIN 41 shares, entry
          entry 961.85 (the plan's entry), stop 950.05, target 985.45: risk 11.80 a share
          max loss Rs 483.80 (limit Rs 1,000.00) at the stop, at most Rs 598.95 with brokerage, STT, exchange and SEBI charges, GST and stamp duty; reward:risk 2.00 (min 1)
          today before this: 0 of 3 trades, Rs 0.00 of Rs 3,000.00 daily loss
        ~ the stop and target are not at the broker yet: Zerodha has no bracket order, so /nse-broker places the exits itself: MIS entry, then an SL-M at the stop after each fill; the target is never placed at Zerodha for MIS (watched by /nse-alerts); CNC/MTF get a GTT (two-leg, or single-leg when stop only)

1 passed, 0 blocked at 2026-10-22 10:20 IST. Cash after the passed orders: Rs 60,564.15 (paper account).
Every order passed. Nothing has been placed; you still decide each one.
Education and tooling, not financial advice.
```

With `config.example.json` as `config.json` and no portfolio file: a fresh 1,00,000 account. The
"at most" figure adds this module's delivery cost rates, each rounded up, so for an intraday (MIS)
trade it is a ceiling; for CNC and MTF it is said to be before the depository charge (and MTF
interest).

Proved against the US pack's own documented example: the same TSLA trade (entry 364.18, stop
358.32, target 370.04) passes at 8 shares with a max loss of 46.88 and reward:risk 1.00, blocks at 9
shares (52.74), blocks twice at a fresh 366.48 (65.28 and 0.44), and blocks at 9:40 by the window;
the closing sell passes because it is an exit. (Here its cent prices, 358.32 and 370.04, are also
refused for being off the NSE's 5-paise tick; every other reason is the reference's.)

## Sizing one idea from scratch

```
risk budget   = capital × risk_per_trade_percent
shares        = risk budget ÷ (entry − stop)
```

Then the size is cut if it exceeds your capital, and the full cost of getting in and out at the stop
is added to the loss. **Costs are not a footnote.** On a tight stop they are a material share of
what you are risking, which is why they are in the output rather than in a disclaimer.

The Indian cost stack, charged on a delivery trade: brokerage, STT on both sides, NSE transaction
charge, the SEBI fee, GST on the brokerage, the exchange charge and the SEBI fee, and stamp duty on
the buy. Every rate is rounded **up**. A sizing tool that flatters the arithmetic is worse than
none, and a discount broker will only make the real figure better than this. With no `capital` in
your config and no `--capital`, it sizes against 1,00,000 and says so in the output every time.

It refuses a stop at or above the entry (long trades only), closer than 1%, further than 25%, when
you already hold `max_positions`, or when one share already risks more than the budget. It warns
when costs exceed 15% of the risk budget (a stop tighter than about 2%), when reward to risk is under 1.5 after costs, and when
the position had to be cut to fit capital. A block always says what would have to change, and never
suggests a different stop: choosing a stop is a judgement about the chart.

```
$ python risk.py --entry 687.40 --stop 650 --target 780 --capital 500000

PASS

   shares                133
   position              91,424.20  (18.3% of capital)
   stop                  650.00  (5.44% away)
   risk budget           5,000.00
   costs, round trip     261.59
   loss if stopped       5,235.79
   gain if target        12,030.12
   reward to risk        2.3

Sizing is arithmetic, not a recommendation. This tool cannot place an order.
```

(With a `config.json` present it also prints which file the other limits were read from.)

```
$ python risk.py --entry 687.40 --stop 685 --capital 500000

BLOCK

   - stop is 0.35% away, closer than 1.0%: inside one minute's range on most Indian mid caps, so it would be taken out by noise and the size it implies is unreal

Nothing sized. Change the stop, the capital or the limit and ask again.
```

## Steps

1. `/nse-budget --write` proposes the orders. If the price may have moved since the plan, refresh
   it first (`/nse-alerts`: `alerts.py quote SBIN --write`), so the risk is measured from it.
2. Run this. Exit 0 means every order passed, 1 that at least one was blocked (or the check could
   not run, and it says why).
3. Tell the user the result in plain words, order by order: the max loss at the stop, reward:risk,
   and how much of today's limits it uses, and every warning. For a BLOCK, give the reason and the
   fix (fewer shares, a closer stop from `/nse-exit`, wait for the window, or skip the trade).
   Never change a limit yourself.
4. If the order changes in any way, run the check again. `/nse-broker` matches each order to its
   verdict by fingerprint, so an edited order is not ready until it passes again, and a verdict
   from another day does not count today.
5. Only on PASS does anything downstream happen, and the user still decides each order.

## Rules

- A PASS only means the order may be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Free prices are unofficial and can lag; Zerodha's price is the one that fills, and a stop can
  fill worse than its price in a fast market, or not at all on a circuit.

## What it never does

- Never chooses an entry, a stop or a target, or suggests a stock.
- Never places or prepares an order, and never edits an order to make it pass.
- Never loosens a limit, and never blocks a sell that only gets the user out.
- Never treats a PASS as the user's yes.
- Never lets a missing config, or a stale price, quietly stand in for the real thing.
- Never passes a short, a future or an option unless your config allows it.

## Safety

A PASS means the order is consistent with your own stated rules; it says nothing about whether the
trade is a good idea. Nothing here is financial advice and no part of this skill can place a trade.
Paper by default (`"paper": true`); going live is the user's own decision, in their own config.
Only `/nse-broker` can lead to an order, and only when all three hold: this gate passed that exact
order today, the user typed an explicit yes for that one order, and the user clicked Allow once on
Claude's own card for Zerodha's `place_order`.
