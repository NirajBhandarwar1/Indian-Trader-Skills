---
name: nse-timemachine
description: >-
  Time Machine. Replays the rest of the day on your plan ten thousand times and
  gives the odds. Reads the plan from /nse-exit, resamples real bars from the
  stock's last 60 NSE sessions at the same time of day, and reports how often the target came
  first, the stop came first, or neither by your flat-by time, with the plan's
  own rule re-run on those same past sessions as the honest check. Says whether
  that is above or below break-even for the target's R, which is not the same as
  a win rate. Use when the user says "what are the odds", "will it hit my
  target", "is this plan any good", "replay this", "what usually happens", or
  after /nse-exit writes a plan and before /nse-budget sizes it.
argument-hint: "run [--replays N] [--until 15:20] [--stop-rule auto|or-mid|or-low|pct] | chart"
---

# nse-timemachine

The Time Machine does not predict anything. It answers one narrow question: **on past sessions
that looked like today, how often did a move like this reach the target before the stop?**

Two answers come back, and they are different things.

| | What it is |
|---|---|
| **The replays** | 10,000 runs built by resampling real bars from past sessions at the same time of day. These give the odds |
| **The history** | the plan's own rule — same opening range, same stop rule, same target in R — run again on those same past sessions, **each day with its own opening range and so its own stop**. Not a simulation: this is what the days actually did, trade by trade |

Scripts live in `${CLAUDE_SKILL_DIR}`.

```bash
python "${CLAUDE_SKILL_DIR}/timemachine.py" run             # nse/plan.json, 10,000 replays, seed 19
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --write     # also save nse/timemachine.json
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:20
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from    # ASCII fan chart of the saved run
python "${CLAUDE_SKILL_DIR}/timemachine.py" chart           # runs the plan again, then draws the fan chart
```

With no `run` or `chart` it prints its help and exits 1, as the US pack does.

**No plan file needed**: give the plan as flags — `--symbol SBIN --entry 961.85 --stop 950.05 --target 2R --date 2026-10-22 --entry-time 10:15`. The same numbers by flags or by file replay identically, and the same checks apply: a stop at or above the entry, a target at or below it, an entry time off the five-minute grid, no entry time at all (there is no default), or a sell (`--side sell`: this pack plans buys only) is refused with the reason. A target given as `2R` is rounded up to a tick, as /nse-exit rounds one. A plan given as flags is out by `--flat-by`, else the config's `flat_by` (`--config FILE`, or `config.json` / `nse/config.json`, as /nse-exit reads it), else 15:20, so it ends where the plan /nse-exit would write ends. `--target`, `--date`, `--entry-time`, `--flat-by`, `--setup` and `--config` belong to a plan given as flags: with a written plan they are refused rather than silently ignored (use `--until` to end the replay earlier). An empty `--until` or `--target` is refused, never taken as the default. `--from` goes with `chart` and `--json` with `run`; each is refused with the other command.

Flags for the history's rule: `--stop-rule auto|or-mid|or-low|pct` (default `auto`, which works the rule out from the plan day's own range: a stop that IS that day's range midpoint or low, to the tick, was set by that rule; anything else is replayed as the same percentage of the entry), `--or-minutes N` (the opening range, default 15), `--setup` (the history replays `orb` only and says so for anything else).

Other flags: `--plan FILE`, `--replays N` (or `--runs`, 10,000), `--window N` (draw past bars from the same time of day ± N bars; 2), `--until HH:MM` (end each replay on the bar closing then), `--flat-by HH:MM`, `--now` (pin the clock: a bar that had not closed by then is not used), `--write [PATH]` or `--out FILE`, `--json`, and `chart --from FILE` to draw a saved result.

`--offline DIR` reads a saved chart, or a folder holding `SYMBOL-60d5m.json(.gz)`. `--seed` changes the draw; the default is fixed so two runs of the same plan give the same answer.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/plan.json` | read | written by `nse-exit` |
| the free chart endpoint | one request: 60 sessions of five-minute bars for `SYMBOL.NS` (`range=60d&interval=5m`), unofficial and keyless, can lag | `--offline` reads a saved copy instead |
| `config.json` | read for `flat_by` only, and only for a plan given as flags | written by you |
| `nse/timemachine.json` | written with `--write`: the odds and the counts behind them, the target's R to four places (`r_at_target`), percentiles, fan-chart bands, the first 160 replays as sample paths, a 24-bin histogram of the price at `--until`, the history, the verdict, the short sessions left out with their bar counts, and `source` and `data` (where the bars came from, the last bar used, and `dropped`: bars not closed by the clock, held back, and missing on the past days) | `chart --from` reads it |

From the plan it reads symbol, date, entry, entry_time and/or entry_bar, stop, stop_rule, target,
setup, opening_range minutes, flat_by, side and interval. `entry_bar` is this pack's bar number as
/nse-entry counts it (1 is the 09:15 bar, closing 09:20); given alone it sets the entry time, and a
plan whose entry_bar and entry_time disagree is refused. A plan file that is not valid JSON, not
a JSON object, or has no date is refused in one line; so is a symbol that is not an NSE symbol, a
price that is true/false, and an opening range that is not a whole number of minutes. A clock time
is HH:MM, 24-hour: 10:60 is refused, never read as 11:00. A bad `flat_by` is named as the plan's,
not as `--until`. `--json` leaves the 160 sample paths out of the screen. A saved result that is
damaged is refused by `chart --from` in one line.

Exit 0 on a result, 2 when there is not enough history, the plan is refused or the result cannot
be written, 1 with no command. A plan for a day the data does not have is told so before the pool
is counted, in the US pack's order.

## How a replay works

1. **The pool.** Past sessions before the plan's date, from the last 60, each with at least 95% of
   the fullest day's bars, and at least 20 of them or it refuses. Half days and today are left out.
   Most of those sessions are NOT complete: the feed leaves bars empty (see below). Every bar
   becomes four ratios to the bar before it — open, high, low and close over the previous close —
   so it can be rescaled to any price, but **only when the bar before it is exactly five minutes
   earlier**. A missing bar is never filled and never bridged: the pair around it is left out.
   Measured on SBIN, bridging the empty 15:20 bar turned the 15:25 bar into a ten-minute move and
   lifted target-first on a 15:30 replay from 15.4% to 20.6%.
   A time of day held by fewer than 20 sessions is left out of the draw (SBIN's 15:20 bar is in 3
   of 60 saved days, and its 15:25 bar in 12, only 3 of them with the 15:20 bar before it, so
   both slots hold 3 of the 58 past sessions). If the replay needs that bar — the entry to `--until` crosses
   it — the run refuses, naming the bar and how many sessions held it; replay to an earlier
   `--until`. The output says how many sessions went in, how many had every bar, how many bar
   pairs were left out for holes, and which times of day were dropped.
2. **One replay.** Start at the entry, on the bar after the entry bar (an entry at the 10:15 close
   replays from the 10:15–10:20 bar), and stop on the bar that closes at `--until`. For each bar, pick a past bar from the same
   time of day (±2 bars) at random and scale it to the current price. **The stop is checked before
   the target**, so a bar touching both counts as the stop. That is the pessimistic reading and the
   right one: from a bar nobody can tell which came first, and assuming the good one flatters
   everything downstream.
3. **10,000 replays** give target-first, stop-first, neither (still open at `--until`, the plan's
   flat-by time unless given), and green (target first, or above the entry at `--until`).
   R is +1 at a 1R target, −1 at the stop, otherwise the distance from the entry divided by the
   risk. A gap through the stop still counts as −1R here. A real fill can be worse.
4. **The verdict is about break-even, not about winning.**

**The draw is the US pack's, draw for draw.** For each bar, a time slot is picked near it
(`randint(-window, window)` bars on the clock, kept inside the day), then one past bar from that
slot. A slot left out of the pool for being thin is replaced by the nearest one held on the way
back towards the bar's own time, so a draw never reaches further than ±window bars. Run against the
US pack's own simulation on the same complete sessions (SBIN and ITC, every day cut to the bars all
of them have), 40 combinations of plan, entry, end time, window, seed and replay count gave the
same counts, percentiles, fan-chart bands, histogram, 160 sample paths, average R, target R and
verdict. A target pays its R to four places, as there. One display difference is kept on purpose:
the US pack rounds each share twice (to four places, then to one), this rounds the exact count
once, so 263 green of 777 replays (33.848%) shows 33.8% here and 33.9% there.

Each replay walks on to `--until` after the stop or target is hit, so the **price at --until** is
reported for all of them: the median and the 5th to 95th percentile. A second run of 2,000 paths on
the same seed gives the bands for the **fan chart** (`chart`, or `chart --from FILE` for a saved
run), and a 24-bin histogram of the price at --until is saved with the result. Under the verdict, a
note when more than 40% of the replays hit neither side, and when the average R points the other
way from the verdict.

## Break-even is the point

A bigger target needs to win **less** often to break even: 1 in (1 + R).

| Target | Break-even hit rate |
|---|---|
| 1R | 50% |
| 2R | 33% |
| 3R | 25% |

So a 2R plan whose target comes first 32% of the decided runs is **not a loser — it is level.**
Reading that 32% as "it fails two times in three" is the mistake this wording exists to prevent.
Within 5 points of break-even it says close to break-even; 5 to 15 points, leans; beyond that,
strongly.

## The history, day by day

On each past session the plan's rule is run again: the first bar after that day's opening range to
close above it is the entry, the stop is set by the rule from **that day's** range (or-mid, or-low)
or as the plan's percentage (pct), the target is the plan's R, and then bar by bar the stop is
checked first, the target second, else the close of the `--until` bar. Each trade is listed with its
date, entry, stop, target, outcome and R, with the totals: triggered, target first, stop first,
still open at `--until`, green, and the average R.

**A missing bar is never stepped over in the history either.** Missing before the break, the first
close above the range may have been in it; missing while the trade is open, the stop or the target
may have been touched in it; missing at `--until`, the price there is not known. Such a day is left
out of the counts and named in the history line with the bar it lacked. Measured on the saved SBIN
days: 6 have no 15:15 bar, and scoring them on the 15:10 close had called a 15:10 price "still open
at 15:20"; with a 15:20 flat-by, 5 of the 58 days are left out (four for the 15:15 bar, one for
an 11:45 bar missing before the range broke) and 53 scored.

Checked against the US pack's own code on the same complete sessions of SBIN and ITC: 768
combinations of stop rule, target R, opening range and end time, 16,592 trades, every trade and
every total identical, the average R included (it is `statistics.mean`, as there; `fmean` had
moved 9 of the 768 averages by 0.001).

## The data, said plainly

- **Sixty sessions.** The free chart endpoint keeps 60 sessions of five-minute bars (measured
  2026-10-24: SBIN 2026-07-29 to 2026-10-23). One-minute bars stop at 8 sessions, and a 30-day
  request is refused, which is why the replay is five-minute only.
- **The session length is measured, not assumed.** NSE's 09:15 to 15:30 is 75 five-minute bars on
  paper, and the feed often sends fewer: in the sixty days saved, 57 of SBIN's sessions came back
  with the 15:20 bar empty (only 3 of 60 had every bar). An empty bar is dropped, never filled,
  and the move across it is never counted as one bar. The fullest day is the yardstick for which
  sessions go in, so a hard-coded 75 cannot reject every day there is.
- **Only closed bars.** A bar that has not closed by the clock (`--now`) is never used. The newest
  bar is also held back while the feed's own last trade is still before its close, for up to a
  minute after it, as /nse-entry holds it: the US pack takes the earlier of the clock and the
  feed's time for the same reason. An entry on that bar is refused with the reason; run it again.
- **Ticks.** A stop or target on a tick stays exactly on it: an opening-range low of 950.05 is a stop
  of 950.05, not 950.00 (the first version floored 19000.999... and moved it a tick).

For a record of a rule over years, use `/nse-backtest`, which works on daily bars. This skill is
about one plan on one day.

## Steps

1. Make sure `nse/plan.json` is today's plan from `/nse-exit`. If there is none, run that first,
   or pass the plan as flags.
2. Run `timemachine.py run --write`. The replay ends at the plan's flat-by time (15:20 by
   default), so "neither" means still open at the time you would get out.
3. Tell the user, in this order: the odds (target first, stop first, neither, green), the median
   and the 5 to 95 range of the price at `--until`, the average R, the history line, then the
   verdict line and any line under it exactly as printed. Say the seed.
4. Say the number of past sessions, how many the history left out for a missing bar, and how many
   times the setup triggered, out loud.
5. If the verdict is a coin flip or close to break-even, say so plainly. Do not dress it up. If the
   history and the replays disagree, say that too.
6. If the odds lean to the stop, the fix is a nearer target or a different setup — **not a tighter
   stop**, which only raises the chance of being stopped out.
7. Say once that this is built from the last 60 sessions and is not a prediction: results, news, a
   circuit or a halt can do things those sessions never did. Check `/nse-news` for the stock
   before going on.
8. If the user still wants the trade, hand on to `/nse-budget` to size it (the run ends with that
   line).

## Example: SBIN, Thursday 2026-10-22

The 15-minute opening range is 950.05 to 959.30. The 10:10 bar closes above it: entry 961.85 at
10:15 (bar 12), stop 950.05 (the range low), target 985.45 (2R, risk 11.80 a share). A real run on
the saved bars (`--offline tests/fixtures/bars --now "2026-10-24 09:00"`):

```
10,000 replays of the rest of the session, 10:15 to 15:20 (61 five-minute bars), seed 19.
  target first  #...............................................   1.7%
  stop first    #########.......................................  19.1%
  neither       ######################################..........  79.2%  still open at 15:20
  green (target first, or above the entry at 15:20): 45.3%
  Price at 15:20: median Rs 961.13, 90% of replays between Rs 947.88 and Rs 976.63.
  Average R: -0.052
What actually happened: ... on each of the 58 sessions ... (5 left out for a bar the feed never
sent before the trade was decided ...; 53 scored). It triggered 29 time(s): target first 3, stop
first 10, still open at 15:20 16; 12 green, average R +0.067.
Strongly toward the stop: at 2R the target has to come first in 33% of the replays that hit one
side, which is break-even; it did in 8% (target first 1.7% vs stop first 19.1%).
79.2% of the replays hit neither by 15:20; the price at 15:20 decides those.
```

The replays lean hard to the stop at 2R, while the real history of the same rule was slightly
positive over 29 trades. They disagree, and the user hears both before `/nse-budget` sizes
anything.

## Rules

- Every number comes from the run. Odds from resampled past bars, never a forecast. Never round a
  coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat them, do not rewrite
  them. Never judge a 2R or 3R plan by target first against stop first alone: the verdict already
  weighs the target's R.
- The stop wins a tie, always.
- Say how many sessions it had. A small pool is reported, not hidden.
- Never present the green percentage as a probability of making money.
- The free chart data is unofficial and can lag. Bars are five-minute bars, checked bar by bar,
  never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan to improve the odds, and
never claims to know what the price will do. It never prints a P&L or a profit. It never uses a bar
that has not closed yet, never mixes the plan's own day or a later one into the pool, and never
bridges a bar the feed did not send.

## Safety

Education and tooling, not financial advice. A circuit, an opening gap and a news halt are in none
of these replays, and each can produce an outcome worse than every one of the 10,000.
