---
name: nse-exit
description: >-
  Exit Planner for NSE stocks. Sets your stop and your target before you enter,
  so you know when to get out. Reads the entry from /nse-entry (or a price you
  give), puts the stop at the opening-range midpoint by default (or the range
  low, your price, a percent, or a multiple of the ATR), rounds it away from the
  entry to a tick the exchange accepts, and sets the target at 2R. Refuses a stop
  at or above the entry, a target at or below it, or a stop too tight to survive
  the spread, and says why. Writes nse/plan.json for /nse-budget and /nse-alerts.
  Long side only. Use when the user says "where's my stop", "set my stop and
  target", "plan the exit", "when do I get out", "stop loss", "what's my R",
  "risk reward", or right after /nse-entry triggers.
argument-hint: "plan [--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# nse-exit

The Exit Planner decides how a trade ends before it starts. The stop is where the setup has
failed. The target is where you take what you came for. Both are set BEFORE the entry, written
down and handed on, so nobody has to decide anything in the middle of a fast move.

A stop is a plan, not a guarantee. Day trading loses most people money, and a neat plan does not
change that.

Scripts live in `${CLAUDE_SKILL_DIR}`.

```bash
python "${CLAUDE_SKILL_DIR}/exit.py" plan                        # from nse/entry.json: midpoint stop, 2R
python "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5 --offline SBIN-5m.json   # needs the session's bars
python "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol SBIN --entry 961.85 --or-high 959.30 --or-low 950.05
python "${CLAUDE_SKILL_DIR}/exit.py" show                        # the current nse/plan.json
```

Other flags: `--flat-by 15:20`, `--interval 1m|5m|15m`, `--setup orb|level` (a level setup has no opening range, so it needs a named stop), `--or-minutes N` (the range the plan carries: the trigger's own length, read from its `minutes` or its `from`/`to`, else 15), `--date YYYY-MM-DD` and `--time HH:MM` (the session and entry time, IST; `10:15 AM` also works; default the trigger's, or today), `--entry-bar N` (the entry's bar, so an ATR stop uses only bars up to it and nothing from after the entry), `--side` (buy; a short is refused with the reason), `--now` (pin the IST clock: `2026-10-22 10:15`, `10:15` for today, or a time with `Z` or an offset, turned into IST), `--offline FILE` (a saved chart, or a folder of them), `--json`, `--from FILE` (the trigger to plan around, instead of `nse/entry.json`), `--write [PATH]` (save the plan, to `nse/plan.json` or PATH), `--config FILE`, and `show --plan FILE [--now] [--json]` to print a saved plan. Giving both `--from` and `--symbol`, or `--or-high` without `--or-low`, is refused. With `--json --write`, the "wrote" line goes to stderr so the output stays JSON. No subcommand prints the help.

A stop or target that lands exactly on a tick stays on it: an opening-range low of 950.05 is a stop of 950.05. The first version floored 950.05 / 0.05 = 19000.999... and moved such stops a tick lower.

Every stop and target line shows its working, exactly and never rounded (the midpoint sum, `Rs 961.85 x (1 - 1%) = Rs 952.2315`, `Rs 961.85 - 1.5 x 3.195833 = Rs 957.0562505`, `Rs 961.85 + 2 x Rs 7.20 = Rs 976.25`) and says "rounded down/up to the tick Rs X" only when rounding actually moved the price. The arithmetic is exact decimal, as the US pack does it, and the ATR is used to six places, as the US pack uses it. A price target also shows the R it gives. An entry that is not on the tick (961.83 where the tick is 0.05) is planned but carries a note, because no order fills there. An entry with a third decimal (961.845) is rounded half up to the paisa (961.85), as the US pack rounds to the cent, and the plan says so.

`--entry-bar` counts from 1 (the 09:15 bar is bar 1, as `/nse-entry` writes `bar_index`), and it is the bar's place on the clock, so a bar missing from the feed never lets a bar from after the entry in. Bar 0 or below is refused: bar 0 once read as "no bar" and sized an ATR stop on the whole session. An entry bar and an entry time that disagree (bar 12 closes 10:15, so not `--time 10:20`) are refused, as `/nse-timemachine` refuses them, and so is an entry bar that closes at or after the flat-by time (bar 73 of a 5-minute day closes 15:20). An ATR stop also leaves out any bar that ends after the chart's own last-trade time, as the US pack does: a chart saved at 11:02 holds an 11:00 bar that had not finished.

The trigger is read from `entry` and `trigger_time`, or, where those are missing, from `price` and `time` (the names the US pack reads; `/nse-entry` writes both).

The stop rules also answer to `mid`, `or-midpoint` and `low`; the plan stores the canonical name (`or-mid`, `or-low`, `price:955`, `pct:1`, `atr:1.5`). The X is plain digits (`1,000.05` and `Rs 955` are read; `1e3`, `nan` and `$955` are refused). A bare `R` is not a target: say `1R`.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/entry.json` | read | written by `nse-entry` |
| `config.json` | read | `flat_by`, `min_reward_risk` (default 1.0, as `/nse-risk`), `min_stop_percent` (default 1.0) |
| `nse/plan.json` | written with `--write` | `nse-budget`, `nse-alerts` and `nse-timemachine` read it |

The plan carries `target_r` (the multiple asked for: 2R stays 2.0) and `target_r_actual` (the R the
tick-rounded target really gives, also `reward_risk`), `stop_label`/`stop_why` (the whole sentence)
and `stop_detail` (the working alone, as the US pack keeps it). `stop_rule` and `target_rule` are
stored as the number, not the typing (`price:Rs 1,000` is `price:1000`, `2.50r` is `2.5R`). An ATR
stop also records `atr` (to four places) and `atr_bars`. A config value for `min_reward_risk` or
`min_stop_percent` that is not a number is refused with its name; a warning says "your" for a value
from the config and "the default" for one that is not set.

The `min_stop_percent` warning uses `/nse-risk`'s own rule, in whole paise: a stop exactly on the
gate passes (SBIN at 965.00 with `pct:1` is 955.35, exactly 1%: no warning) and one a paisa inside
it is warned (955.40). The wider stops it suggests are only ones it worked out
and checked against the same gate: `or-low` only when there is a range and it passes, `pct:X` only
at an X that passes. The `min_reward_risk` warning compares the exact ratio, as `/nse-budget`
refuses on it: 9.95 on a 10.00 risk is 0.995, shown as 1.0 in the plan, and warned as `0.9950`.

Exit 0 on a plan, 2 on a refusal.

## Stops and targets

| stop | where it goes |
|---|---|
| `or-mid` | the opening-range midpoint — the default for an opening-range breakout |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry. With fewer than 14 closed bars it averages the ones there are and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

Risk = entry − stop. Target = entry + R × risk, `2R` by default; `1R`, `1.5R` or `price:X` also
work.

## Rounding to a tick the exchange accepts

A stop at an impossible price is an order that gets rejected, so every price is rounded to a real
tick — **the stop down and the target up**, never towards the entry. Rounding a stop towards the
entry would quietly shrink the risk a share and buy more shares than your limit allows.

The tick, measured from a full day's traded prices across 2,706 symbols:

| Price | Tick |
|---|---|
| below ₹250 | **₹0.01** |
| ₹250 and above | **₹0.05** |

## Refused, with the reason

- A stop at or above the entry, or a target at or below it.
- **A stop too tight to survive the spread.** The floor is the wider of 0.05% of the price and two
  ticks. The tick part is the Indian half: 0.05% of a ₹39 stock is under two paise, which is less
  than one tick and would be a stop sitting at the entry.
- An `nse/entry.json` that has not triggered, unless you pass `--entry`.
- An entry at or after the flat-by time: there is no trade left in the day. An entry before the
  09:15 open.
- A short. Long side only.

Also refused: a `pct:` stop of 0 or 100% or more, a `price:` or `atr:` of 0 or less, a target of 0R
or less, anything that is not a number, a number of a trillion or more (no price, R or setting is
that large), an opening-range high at or below its low (whatever the stop rule, so a plan never
carries a range upside down), a range longer than the 375-minute session, a stop that works out at
or below zero, a saved trigger for a short, and an entry at or after the flat-by time (compared as
times, so `9:45` is before `15:20`). Measured before this check existed: `--stop pct:150` produced a
stop of Rs -480.93.

A damaged file is a refusal, never a traceback: a missing `--from` file, a trigger or plan that is
not valid JSON or not an object, a trigger whose symbol, setup, date, interval or time is not text,
an opening range without its high or low, a price that is not a number, an entry of 0 in `show`, a
date (from a flag, the saved trigger or the plan) that is not YYYY-MM-DD, and an `--offline` file
that is missing, not JSON, a cut-off or corrupt `.gz`, or holds another stock or bar size. A
`--config` or `--write` that names a folder is refused saying so, and a plan that cannot be written
says so and prints nothing as written. `show` also refuses a plan that contradicts itself (a stop at
or above the entry, a target at or below it, a risk a share that is not entry less stop, a
reward:risk its own entry, stop and target do not give) or that would print something that is not
a plan's (an interval, entry time, entry bar or flat-by that is not one, an entry bar and entry
time that are not the same moment, an empty symbol), because
printing it would show a trade that is not the one planned. A field saved as null is shown as the
default (orb, 5-minute), never as "None".

## Flat by

**15:20 IST by default.** The exchange closes at 15:30, but most Indian brokers square off an
intraday (MIS) position themselves at around 15:20, so the default leaves room. That is a broker
convention and not an exchange rule — change `flat_by` in `config.json` if yours differs.

The flat-by time must fall inside the session: a `--flat-by` (or a config `flat_by`) at or after
the 15:30 close, or at or before the 09:15 open, is refused. `show` refuses a plan missing any of its fields and
says when the plan is not for today, worked out against the clock when you run `show`: the "not
today" line saved when the plan was made is never reprinted.

## Steps

1. Run it right after `/nse-entry` triggers. Before that, `--entry PRICE` plans ahead at the level
   you expect.
2. Use the default stop for an opening-range breakout unless the user names another. A level setup
   needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule, the target and its rule,
   the risk a share, reward:risk and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther target). **Never loosen
   anything on your own.**
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/nse-budget` to size it, then `/nse-risk`. Offer `/nse-alerts` for the stop, the
   target and the flat-by time.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only. It never prints a P&L or a return.
- A 1R target is not a prediction that the price gets there.
- Long side only.

## What it never does

It never places an order, never sizes a position, never widens a stop to make a plan pass, and
never calls a stop a guarantee.

## Safety

Education and tooling, not financial advice. A gap at the open or an **upper or lower circuit** can
fill a stop far past your price, or leave it unfilled while the stock is frozen. That is the one
thing a stop cannot protect against.
