---
name: nse-scan
description: >-
  Screener. Scans the whole NSE market for setups firing today and prints each
  rule's measured record beside its candidates: trade count, average return, and
  edge over buying at random. Says plainly when a rule lost money. Use when asked
  what is setting up, to screen the market, or to find candidates for a rule.
argument-hint: "[--setup NAME | --all] [--on YYYY-MM-DD] [--top N] [--json]"
---

# Screener

Scans every cached stock for a rule firing today.

The part that matters is not the list. **Every result carries what `/nse-backtest` measured for that
rule** — how many trades, what it averaged, and its edge over simply buying anything. A screener
that lists candidates without the rule's history is inviting you to act on a rule nobody checked,
and that is the failure this whole pack is built around not repeating.

When a rule's record is bad, the candidates are still listed and the header says so. Hiding them
would be a different kind of dishonesty.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python ${CLAUDE_SKILL_DIR}/scan.py --all                        # every rule, best record first
python ${CLAUDE_SKILL_DIR}/scan.py --setup delivery-spike       # one rule
python ${CLAUDE_SKILL_DIR}/scan.py --all --on 2026-10-15        # a past cached day
python ${CLAUDE_SKILL_DIR}/scan.py --all --save                 # also write nse/scan.json
```

Needs the price cache: `python nse-backtest/backtest.py --fetch 250` once. Scans are offline after
that. Exit 0 on success, 1 if no rule was named.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/prices.sqlite` | read | written by `/nse-backtest --fetch` |
| `nse/scan.json` | written with `--save` | hand candidates to `/nse-risk` |

The setups are **imported from the replay**, not copied. A rule screened on one definition and
measured on another is worse than no measurement, and a test asserts the screener keeps no copy of
its own.

## When a rule is not worth acting on

The header says so, for any of four reasons:

| Reason | Why it disqualifies |
|---|---|
| never measured | run `/nse-backtest --setup NAME` first |
| **edge ≤ 0** | the rule lost to buying at random over the window measured |
| **under 100 trades** | not enough to claim a win rate from |
| **median at the stop** | more than half lose; the average is a few outliers |

A hundred trades, not thirty. Thirty is the rule of thumb for a mean behaving normally and is far
too few to claim a win rate from — below about a hundred, the number moves several points if one
trade goes the other way.

**All four shipped setups currently fail at least one of these.** The screener says it outright:

```
None of these rules has a record worth acting on. A candidate list is not a reason to buy.
```

That is the honest state of them, not a bug.

## Example

```
$ python scan.py --all --top 3

=== delivery-spike ===
   measured: 57 trades, 33.3% win rate, +0.78% average against a baseline of
   -1.89%, edge +2.67pp  (2026-07-13 to 2026-10-23)
   Only 57 trades measured, under 100: not enough to claim a win rate from.

   Nothing fired today.

=== breakout ===
   measured: 5,235 trades, 26.9% win rate, -1.36% average against a baseline of
   -1.89%, edge +0.53pp  (2026-07-13 to 2026-10-23)
   The median trade hit the stop: more than half lose, and the average is
   carried by a few outliers.

symbol        close       change%    delivery%   turnover
------------  ----------  ---------  ----------  -----------
CYIENT        1233.0      7.81       10.5        245594.6
TCS           2530.0      0.92       34.4        176079.8
SSWL          468.75      19.99      15.6        88172.2
```

Note CYIENT: up 7.81% on 10.5% delivery. A big move almost nobody kept. `/nse-delivery` is the skill
for that question.

## Data and limits

- **Daily bars only.** Nothing here can see inside a session.
- **Rules are ordered by measured edge**, best first, so the worst sit at the bottom where they
  belong.
- **An illiquid stock is never a candidate**, the same 100 lakh turnover floor the replay uses.
- **`--on` only accepts a cached day** and refuses anything else rather than returning an empty
  list that looks like "nothing fired".
- A rule needs more cached days than its lookback window before it can fire at all.

## Steps

1. `/nse-backtest --fetch 250` once, then measure each rule you care about.
2. `--all` to see what fired, with each rule's record.
3. **Read the header before the table.** If the rule lost money, the table is a curiosity.
4. For a candidate worth following up: `/nse-delivery SYM`, `/nse-insider SYM`, `/nse-news SYM`.
5. `/nse-risk` to size it. That is the only skill that can say no.

## Rules

- Print the rule's record above its candidates, always.
- List the candidates even when the record is bad, and label the record.
- Order rules by measured edge, never alphabetically.
- Never present an unmeasured rule as though it had been checked.

## What it never does

- Never names a stock to buy or sell.
- Never hides a rule's losing record to make a list look better.
- Never invents a setup the replay cannot measure.
- Never places or sizes an order.

## Safety

A candidate list is not a reason to buy. Every rule shipped here currently fails at least one test
of whether its record means anything, and the screener says so. Nothing here is financial advice.
