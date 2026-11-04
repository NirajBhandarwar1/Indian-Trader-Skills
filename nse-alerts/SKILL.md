---
name: nse-alerts
description: >-
  Price Alerts for NSE stocks. Rings when a price, a stop or the opening bell
  hits. Turns the trade plan into three alerts in one go (the stop, the target
  and flat-by), plus above / below / percent moves from the previous close, any
  time of day, and the opening bell, each ringing once so you are not spammed.
  Knows the NSE clock, fetches the exchange's own holiday list, works out IST
  itself, fetches free keyless delayed quotes, and writes nse/prices.json for
  sizing. Use when the user says "alert me when", "tell me if SBIN hits", "watch
  my stop", "remind me to square off by 3:20", "is the market open", "when does
  the market open", "get me a price", or right after /nse-exit writes a plan.
argument-hint: "clock | add --from-plan | check | quote SYMBOL"
---

# nse-alerts

Price Alerts watches the numbers so the user does not have to stare at a chart. **An alert is
information, never an order:** when a stop alert rings, nothing is sold by this skill.

Scripts live in `${CLAUDE_SKILL_DIR}`.

```bash
python "${CLAUDE_SKILL_DIR}/alerts.py" clock                        # open or closed, next open
python "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan              # stop, target and flat-by
python "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol SBIN --stop 950.05
python "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol SBIN --target 985.45 --rearm --note "first target"
python "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol SBIN --above 970 --date 2026-10-22
python "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:20 --label "square off" --symbol SBIN
python "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol ITC --pct 3 --direction up
python "${CLAUDE_SKILL_DIR}/alerts.py" check                        # exit 0 = something rang
python "${CLAUDE_SKILL_DIR}/alerts.py" quote SBIN --write           # -> nse/prices.json
python "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write     # the plan and the portfolio
python "${CLAUDE_SKILL_DIR}/alerts.py" list
python "${CLAUDE_SKILL_DIR}/alerts.py" reset sbin-plan-stop
```

Flags, by command:

- **add**: `--symbol`, one of `--above`, `--below`, `--stop`, `--target`, `--pct` (with
  `--direction up|down`), `--at HH:MM`, `--opening-bell`, or `--from-plan` (with `--plan FILE`, and
  `--config FILE` for a flat-by time when the plan has none); then `--date` (watch on that day
  only), `--rearm` (ring again once the price has moved back across the level), `--label`,
  `--note`, `--id`, and `--rules FILE` / `--state FILE` for where the rules and their state live.
- **check**: `--quiet` (say only what rang, which suits a `/loop`), `--json`, `--no-write` (look
  without recording anything), `--prices-out FILE` (where the quotes it fetched are merged),
  `--source feed|exchange`, `--offline DIR` (saved `SYMBOL.json` or `SYMBOL.json.gz` quotes, and
  the built-in holiday list),
  `--now "2026-10-22 15:20"` (pin the IST clock; an ISO time ending `Z` is UTC), `--rules`,
  `--state`. Exit 0 when something rang, 1 when nothing did or a file or rule was refused.
  Any command exits 2 for a `--now` it cannot read, and `add --from-plan` for a config file that
  is not valid JSON (a `--config` file that does not exist is exit 1).
- **quote**: `--write [PATH]` (merge into `nse/prices.json`, or PATH), `--holdings` with
  `--portfolio FILE` and `--plan FILE`, `--source`, `--offline`, `--now`, `--json`.
- **reset ID** (`--state`), **list** (`--json`, `--rules`, `--state`), **clock** (`--now`,
  `--offline` for the built-in holiday list, `--json`; exit 0 open, 1 closed).

The rules file is `{"rules": [...]}` (a bare list is still read; `alerts.example.json` is one, in
the shape `add` writes) and the state `{"rung": {id: when}}`. A rules or state file that does not
parse or has any other shape is **refused, never read as empty**: read as empty, the next `add`
wrote over the rules, and an empty state rang every alert again. So is a rule that cannot mean
anything (a price of 0, nan or infinity, a pct of 0, a direction other than up or down, a date
that is no day, an id that is not text) and two rules under one id.

`quote --write` **merges** into `nse/prices.json`. It used to replace it, so quoting one symbol deleted every other price and `/nse-budget` lost them without a word.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/plan.json` | read | written by `nse-exit`, for `add --from-plan` and `quote --holdings` |
| `nse/portfolio.json` | read | written by `nse-broker fill`, for `quote --holdings` |
| `config.json` or `nse/config.json` | read | `flat_by` when the plan has none (`--config FILE` names one) |
| `nse/alerts.json` | read and written | the rules |
| `nse/state/alerts.json` | read and written | which rules rang, so each rings once |
| `nse/prices.json` | merged by `quote --write` and by `check` (`--prices-out`) | `nse-budget` and `nse-risk` read it, by each price's own `detail.at` |

## Rule types

| type | rings when | rings again |
|---|---|---|
| `above` / `target` | price at or above `price` | after `reset`, or on its own with `"rearm": true` |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`, else the day's open), optional `direction` | once a day at most |
| `time` | the IST clock passed `at` on a trading day | once a trading day |
| `opening_bell` | the NSE is open | once a trading day |

A rule with a `date` is watched on that date only. `--from-plan` rules carry the plan's date, **so
yesterday's stop never rings today** against a price that has nothing to do with it. Plan alerts are
a long's: a plan whose side is not buy, or whose stop is not below its target, is refused, because
a short's stop sits above the price and its "at or below" alert would ring at once.

## The NSE clock

Regular session **09:15 to 15:30 IST**, with a pre-open auction from 09:00. Closed at weekends and
on about sixteen holidays a year.

**The holiday list is fetched from the exchange, not written into the code.** Several Indian market
holidays move with the lunar calendar — Holi, Id-Ul-Fitr, Bakri Id, Muharram, Diwali — so a list
hard-coded today is wrong next year, and a wrong holiday list tells you the market is open when it
is shut. A small copy is kept as a fallback for when the network is down, and it names the year it
is good for rather than pretending to be current.

**Muhurat trading** is the Indian answer to a half day, and it is the opposite of one: on Diwali
Laxmi Pujan the market is closed and then opens for about an hour in the evening for a ceremonial
session. The exchange announces that hour separately. This skill reports the day as closed and
says a special session may be announced — it never guesses the hour.

**IST never changes.** India has no daylight saving, so the clock needs no timezone files.

## Quotes

Free, keyless and **delayed**. Fine for an alert and for checking a size, never for timing an
order: the broker's price is the one that fills. Each price keeps its own quote time so a stale
one can be spotted. Requests go one symbol at a time, half a second apart; a 429 (too many
requests) waits two seconds and asks once more, then the second host is tried. A quote whose price
is missing, zero, negative or not a number is refused, never rung on, and one with no readable time
is set aside as unverified.

**Two sources, and the fallback is the better data.** The intraday feed is tried first because it
is current; if it is down, the exchange's own closing file answers instead. That ordering is
deliberate — the fallback is *the record*, just a day old, and every quote carries a `source` line
saying which one it came from. A fallback price is never presented as live.

`quote --holdings` quotes everything worth watching at once: the open plan and anything in
`nse/portfolio.json`.

**A price rule never rings on yesterday's price.** When the intraday feed is down the quote falls
back to the exchange's closing file, which is a day old. A stop that rang on it during the session
would say SBIN "is" at a price it was at yesterday. So a quote whose own date is not today is set
aside, no price rule is checked on it, and `check` says so. Every ring carries the quote's time:
"SBIN is Rs 958.60 (quote 15:28 IST): at or below your stop Rs 960.00" (the real SBIN quote of 23
October 2026 against a stop at 960). Plan alerts carry the plan's
own words for the stop and the target.

## Steps

1. Right after `/nse-exit` writes the plan, run `add --from-plan`. Confirm the three alerts back:
   the stop, the target and the flat-by time.
2. Run `check` while the trade is open — every few minutes on a `/loop`. Each run fetches only the
   symbols in today's rules and rings each alert once. **A check reads a delayed quote; it is not
   a live feed, and a price can pass a level between two checks.**
3. Say what rang in one line each. "Flat by" means close anything still open — the user does that
   with their broker, not with this pack.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is live, and never predicts
where a price is going.

## Safety

Education and tooling, not financial advice. A stock that hits its **upper or lower circuit** stops
trading, so a stop alert may never ring even though the price reached your level — the quote
freezes at the band.
