---
name: nse-budget
description: >-
  Budget Manager for NSE stocks. Sizes every position to your capital and your
  max loss per trade. Reads the trade plan from /nse-exit (entry, stop, target),
  finds the whole shares that fit inside four limits (max loss per trade,
  position cap, max order, cash after your buffer), says which limit decided it,
  and writes one bracket order to nse/orders.json. A stop too far away for your
  max loss is a plain refusal, not a smaller order. Nothing is placed. Use when
  the user says "how many shares", "size this trade", "how much should I buy", "I
  have 1 lakh", "I only want to risk 500 a trade", "set my rules", or after
  /nse-exit writes a plan.
argument-hint: "[--limit [PRICE]] [--write] | --weights SBIN=40,ITC=60"
---

# nse-budget

The Budget Manager turns a plan ("buy SBIN at 961.85, stop 954.65") into an exact order ("buy 41
shares"), inside rules you set once. **The stop decides the size:** the further away the stop, the
fewer shares.

It writes a proposal only. `/nse-risk` checks it afterwards. Day trading loses most people money.
Sizing to a max loss keeps each loss small; it does not make a trade win.

Scripts live in `${CLAUDE_SKILL_DIR}`.

```bash
python "${CLAUDE_SKILL_DIR}/size.py"                   # read nse/plan.json and config.json
python "${CLAUDE_SKILL_DIR}/size.py" --write           # write nse/orders.json
python "${CLAUDE_SKILL_DIR}/size.py" --limit --write   # a limit order at the plan's entry
python "${CLAUDE_SKILL_DIR}/size.py" --limit 961.00 --json
python "${CLAUDE_SKILL_DIR}/size.py" --weights SBIN=40,ITC=60           # split, below
python "${CLAUDE_SKILL_DIR}/size.py" --targets "${CLAUDE_SKILL_DIR}/targets.example.json" --prices nse/prices.json
```

Weights mode also takes `--paise` (or `--cents`: split to the paisa rather than whole rupees, as the US pack's `--cents` does) and `--whole-shares` (refused when there are no prices at all, from `--prices` or `nse/prices.json`; Indian equities have no fractions, so prices always give whole shares).

Other flags: `--plan FILE` (instead of `nse/plan.json`), `--limit` alone or `--limit plan` (a limit at the plan's entry), `--write [PATH]` (write the orders to `nse/orders.json`, or PATH; written whole or not at all), `--budget N` (override the capital in the config), `--portfolio FILE` (your account; its cash caps the size, default `nse/portfolio.json`), `--top N` (weights mode: keep only the N biggest weights), `--prices FILE`, `--config FILE`, `--json` (JSON only, refusals included; with `--write` it carries `wrote`).

**If your config sets only `capital`, that is what is sized against** (`budget` is the same setting under the US pack's name, and wins when both are set; a `budget` left null falls back to `capital`). **With no capital or budget in the config and no `--budget`, it refuses**, as the US pack refuses: there is no default capital, and a portfolio's cash only caps the size, it is never taken for the capital. Ask the user for the real figure.

`--now "2026-10-22 10:20"` pins the clock (IST). A plan for another day is refused: make a new one
with `/nse-exit`.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `config.json` | read | your rules — start from `config.example.json` |
| `nse/plan.json` | read | written by `nse-exit` |
| `nse/prices.json` | read, optional | written by `nse-alerts` |
| `nse/portfolio.json` | read, optional | its cash caps the size |
| `nse/orders.json` | written with `--write` | `nse-risk` checks it |

Exit 0 on a size (or a split, including one in rupees for reading only), 2 on a refusal.

## Step 1: set the rules once

If `config.json` does not exist, copy `config.example.json` and ask the user for each value in
plain words. All money is rupees.

- **capital**: how much this is for. Only money they can afford to lose. Required: there is no
  default, so without it (or `--budget`) nothing is sized.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **risk_per_trade_percent** (default 1), or **max_loss_per_trade_rupees** to set it absolutely.
- **max_position_pct** (75), **max_order_rupees** (40,000), **min_order_rupees** (500).
  As in the US pack, 0 or null for any of these (or for **min_reward_risk**) means no such limit;
  a negative cap or max order, or a value that is not a number, is refused. A split (weights
  mode) needs a cap above 0 and at most 100, as the US pack's does.
- **min_reward_risk** (1.0), **no_entries_before** (09:30), **no_entries_after** (15:00),
  **flat_by** (15:20), all IST.
- **blocked**: symbols they never want to trade: a list, or one string of names split on commas.
- Keep **paper** true until they have watched it work.

## Step 2: size the trade

**Whole shares only.** Indian equities have no fractional shares at all — not at any broker, for
any product — so every limit floors. The size is the smallest of four:

| limit | shares |
|---|---|
| max loss | max loss ÷ risk a share (entry − stop) |
| position cap | capital × `max_position_pct` ÷ entry |
| max order | `max_order_rupees` ÷ entry |
| cash | (capital − buffer) ÷ entry, never more than the cash in `nse/portfolio.json` |

A position cap or max order set to 0 or null is left out of the table, as the US pack leaves it
out; the cash limit is always there.

**The price it sizes at:** the limit price with `--limit`. Otherwise the plan's entry — or the
price in `nse/prices.json` if it is fresh (under 15 minutes old) **and higher**. A price that ran
up since the plan means a bigger loss at the same stop, so fewer shares. A lower price never
shrinks the risk.

It refuses, and writes no order, when the size is zero shares, the order is under the minimum,
reward:risk is under `min_reward_risk`, the stop is not below the entry, the target is not above
it, the plan is for another day, the symbol is blocked, or the plan is a short. Reward:risk is
worked in whole paise, so a trade of exactly 1R passes a minimum of 1 (in floating point it came
out 0.99999 and was refused).

**A price the exchange does not take is refused, never moved.** A `--limit`, stop or target off
the NSE tick (₹0.05 from ₹250, ₹0.01 below) is refused with the nearest valid prices named:
`--limit 961.83` says 961.80 or 961.85. Moving it for you would change the price you pay or the
risk the size was worked from.

**A price is fresh by its own quote time, nothing else.** `nse/prices.json` is merged by
`/nse-alerts`, so quoting ITC rewrites the file's time while SBIN's price in it can be yesterday's.
Each price is judged by `detail.SYMBOL.at` only, fresh if it is under 15 minutes old, a minute
either side allowed. A price with no `detail.at` is unverified and never used, and that is said. A price that is used
is shown with its quote time. A `--prices FILE` that does not exist is refused, as the US pack
refuses it; one that holds no table of prices is said: no live price was checked. (A limit order
looks at no price, so `--prices` does not matter with `--limit`.)

A plan, config or weights file that is missing, not JSON, or holds a value that is not a number is
a plain refusal naming it, never a traceback; so is a prices file that is not JSON or is named and
not there, a `--now` it cannot read, and a figure too large to be real (a capital or limit over
Rs 10 lakh crore, a share price over Rs 1 crore), where floating point stops counting whole paise.
With `--json` every refusal is JSON, except a flag that cannot be read at all (`--budget nan`,
`--top x`), which gets the usual usage error and exit 2. A number written as text (`"958.6"`) is read as the number.

**A portfolio whose cash is not a number is refused**, as `/nse-risk` refuses it, rather than
sized past. **The cash buffer comes off the cash in your portfolio too.** With Rs 30,000 in hand and a Rs 10,000
buffer, the cash limit is Rs 20,000. Shares are counted in whole paise, so floating point never loses
one, and a plan without a positive entry, stop and target is refused rather than sized.

## Step 3: show the user

The report prints the plan's stop and target with their reasons, every limit with its math in full
(the deciding one marked `<- decides`), **which one decided the size**, the shares, the rupees, the max loss at the stop, the proposed order (`buy 41
SBIN, MARKET, MIS, day, take-profit Rs 985.45, stop-loss Rs 950.05`) and the flat-by time in IST.
Show the user all of it. Say plainly that a stop is an order, not a promise. Then hand to
`/nse-risk`; `/nse-broker` asks the yes or no after it. A refusal for the size (zero shares,
reward:risk, minimum order) still prints the plan, the price it was sized at and every limit, as
the US pack does, then the reason. The orders file is stamped with the `--now` time when one is pinned, and its `why`
names the setup in words (`opening-range breakout on SBIN: 41 shares, bound by your max order`).

## Worked example

SBIN on Thursday 2026-10-22. Opening-range breakout: entry ₹961.85 at 10:15, stop ₹954.65 (the
opening-range midpoint), target ₹976.25 (2R). Risk ₹7.20 a share. Config: ₹1,00,000 capital, 10%
cash buffer, 1% max loss.

| limit | the math | shares |
|---|---|---|
| max loss | ₹1,000 ÷ 7.20 | 138 |
| position cap | 75% of ₹1,00,000 ÷ 961.85 | 77 |
| max order | ₹40,000 ÷ 961.85 | **41 (decides it)** |
| cash | ₹90,000 ÷ 961.85 | 93 |

41 shares = ₹39,435.85. Max loss at the stop: 41 × 7.20 = ₹295.20.

Note what the limits say about a ₹1,00,000 account: on a ₹962 share, the **order cap binds long
before the risk cap does**. On a cheaper stock the max loss binds instead. Which limit decides is
the useful output, not the share count.

## Weights mode: splitting capital across a list

A different job from sizing one trade. There is no stop here, so **nothing decides the size except
the weights and the limits.**

```bash
python "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=90,ITC=5,SBIN=5
```

The cash buffer comes off the top, weights are renormalised to 100, anything over
`max_position_pct` is **pinned at the cap and its excess shared out** among the rest — repeatedly,
because shifting the excess can push another name over in its turn. Slices under
`min_order_rupees` are dropped rather than placed as dust. With prices — `--prices`, or
`nse/prices.json` when it exists, as plan mode uses it — it buys whole shares, by the US pack's
rule exactly: a slice under `min_order_rupees` (a slice that cannot buy one share comes to Rs 0,
so with any minimum it is one) is dropped, saying why, and its money re-split among the rest; with
a minimum of 0 such a name stays at 0 shares, with no order, and that is said too. If nothing is
left it says which reason dropped each name. A price that is not a number above 0, or not on an
NSE tick, is refused. Every name needs a price: the `--targets` example above names TCS, HDFCBANK
and SUNPHARMA, so quote them first (`/nse-alerts quote RELIANCE TCS HDFCBANK ITC SBIN SUNPHARMA
--write`) or it refuses, naming the ones missing.

**Every price a split uses is shown with its own quote time.** The US pack splits at whatever the
prices file holds, however old. Here a price with no `detail.at`, or one not from the last 15
minutes, is called that on the first line and in each order's `why`: the share counts were worked
from it, and a market order fills at the price then. The report ends with what was invested, the
cash buffer and what is **left idle**, never hidden, and the proposed orders. `--targets` takes `{"weights":
{"SBIN": 40}}` or the list form `{"weights": [{"symbol": "SBIN", "weight": 40, "name": "State Bank
of India"}]}` (a name is shown beside its symbol); `--top` must be 1 or more.

On a Rs 1,00,000 budget with no prices, that example gives RELIANCE Rs 75,000 (capped at 75% of
the budget, from a 90% weight) and ITC and SBIN Rs 7,500 each: the 90,000 after the buffer, with
the 15,000 over the cap shared out by weight.

**These are plain buys with no stop.** `/nse-risk` refuses a trade without one and counts each
against the day's trade limit, so use a separate `--config` for a long-term split rather than your
intraday rules.

## Rules

- Proposals only. This skill never calls a broker.
- Never size money the user has not said is for this. Ask for the capital and the max loss; with
  neither `capital` nor `budget` in the config it refuses rather than assume a figure.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free prices are unofficial and can lag. The broker's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its own, never shrinks a
stop to fit more shares, and never sizes options, margin or short positions.

## Safety

Education and tooling, not financial advice: every report ends with that line. Paper by default. Only /nse-broker can lead to an
order, after /nse-risk and your yes. Costs — brokerage, STT, exchange fees, GST, stamp duty and SEBI charges — are `/nse-risk`'s
job, and on a small intraday trade they can be a large share of the move.
