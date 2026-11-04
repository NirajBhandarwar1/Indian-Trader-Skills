---
name: nse-scan
description: >-
  Chart Scanner and screener for NSE. Builds the list of NSE-listed companies
  (EQ series) from the exchange's equity master list, runs the daily scan
  (20-day closing-high breakout, bull flag, reclaimed the 20-day average,
  oversold, oversold bounce, and the delivery rules) with each rule's measured
  record beside its candidates, and an intraday opening-range breakout scan on
  5-minute bars, then writes nse/watchlist.json for nse-news and nse-entry. Says
  plainly when a rule lost money. Use when the user says "scan the market", "find
  me setups", "what is breaking out", "opening range breakouts", "scan for bull
  flags", "oversold stocks", "pre-market scan", "build my watchlist", "what should
  I look at today", or at the start of a trading day before /nse-news.
argument-hint: "[universe | daily | orb] [--setup NAME | --all] [--symbols A,B] [--at HH:MM]"
---

# Screener

Scans the market for charts that match a rule. A scan is a list of charts that match a rule, not
a list of stocks to buy, and a setup on a chart is not a prediction.

The part that matters is not the list. **Every result carries what `/nse-backtest` measured for that
rule** — how many trades, what it averaged, and its edge over simply buying anything. A screener
that lists candidates without the rule's history is inviting you to act on a rule nobody checked,
and that is the failure this whole pack is built around not repeating.

When a rule's record is bad, the candidates are still listed and the header says so. Hiding them
would be a different kind of dishonesty.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python "${CLAUDE_SKILL_DIR}/scan.py" --all                        # every rule, best record first
python "${CLAUDE_SKILL_DIR}/scan.py" --setup delivery-spike       # one rule
python "${CLAUDE_SKILL_DIR}/scan.py" --all --on 2026-10-15        # a past cached day
python "${CLAUDE_SKILL_DIR}/scan.py" --all --save                 # also write nse/scan.json
python "${CLAUDE_SKILL_DIR}/scan.py" --setup oversold             # RSI 14 under 30
python "${CLAUDE_SKILL_DIR}/scan.py" universe                     # write nse/universe.txt
python "${CLAUDE_SKILL_DIR}/scan.py" daily --live                 # the daily scan with no cache
python "${CLAUDE_SKILL_DIR}/scan.py" daily --live --symbols SBIN,ITC   # or --watchlist FILE, --limit 200
python "${CLAUDE_SKILL_DIR}/scan.py" orb                          # after 09:35 IST: the whole universe
python "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15               # cut today's clock at 10:15 IST
python "${CLAUDE_SKILL_DIR}/scan.py" orb --symbols SBIN,ITC       # or a list
python "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist nse/watchlist.json --json
python "${CLAUDE_SKILL_DIR}/scan.py" orb --at "2026-10-22 10:15"  # replay a past session
```

`orb` flags: `--until HH:MM` (no new entries after; default `no_entries_after`, 15:00),
`--max-confirm N` (stage-two checks, 200; 0 = all), `--min-volume N` (shares so far, 0),
`--min-volume-x`, `--min-price` (Rs 20), `--or-minutes` (15), `--pause` (seconds between
requests, 0.3), `--show N` (rows printed, 50; all are written), `--quiet`, `--out FILE`,
`--no-write`, `--config FILE`, `--offline DIR`, `--now "YYYY-MM-DD HH:MM"` (pin the IST clock).
`--symbols` takes commas or spaces; `--symbols` and `--watchlist` cannot be given together. With
no list, `orb` and `daily --live` scan `nse/universe.txt` (run `universe` first; without it they
say so and stop), and offline with no list, every chart in the saved files. Counts (`--limit`,
`--max-confirm`, `--show`, `--top`) and `--pause` must be zero or more.

## Two scans, and they answer different questions

| | When | What it asks |
|---|---|---|
| **daily** (the default) | before the open, on cached daily bars (with no cache or `--live`, the market's daily closes) | which stocks a rule fires on, with that rule's measured record beside it |
| **orb** | during the session, on live intraday bars | which have **CLOSED a 5-minute bar above their opening range** today |

`orb` feeds the rest of the chain: it **writes `nse/watchlist.json`**, which `/nse-entry` reads.
The daily scans write the same file, for `/nse-news` and the others.

## orb covers the whole market, in two stages

**Stage one** asks for closes **twenty symbols a request** — all 2,359 companies in 118 requests.
**Stage two** fetches full bars for the shortlist and applies the real rule.

Two stages because the batch endpoint, measured, returns **closes and nothing else** — no high, no
low, no volume — and the opening-range high is the highest *high* of the range bars. A close cannot
decide that. It can only rule stocks out, and it does so **without losing any**: a bar's close is
never above its own high, so the range's highest close sits at or below its true high, and anything
that closed above the true high necessarily clears the shortlist. The tests check that on real
sessions at six different hours rather than trusting the argument. The test is the US pack's own:
a later close **at or above** the range's highest close (a close level with it cannot break by
itself, but it is counted as a candidate, as the reference counts it). The batch closes also match
the full-bar closes exactly, bar for bar.

Stage two takes the **most traded first**, by the **previous** trading day's turnover from the
exchange's own file — never the same day's, which is not known at 11:00. Anything under the
pack-wide 100-lakh liquidity floor is dropped and counted. Without this, a live run of 2026-10-23
handed the stage-two slots to the stocks whose closes jumped furthest, which were almost all untradeable.
Only a **known** turnover under the floor drops a stock: one the exchange's file gives no figure for
(listed since, say) is kept and ranked after every known one, never called thin on a number nobody
published; `--json` counts them as `no_turnover_figure`.

With no turnover from a trading day **before** the scanned one (a replay of saved files from that
day or later, or the exchange unreachable), nothing is dropped for liquidity and the shortlist is
ranked by how far each close led; the scan says so.

**Nothing is dropped quietly.** If the shortlist is longer than `--max-confirm`, the scan prints
`PARTIAL:` with how many were not checked and their names, and records them as `not_checked` (and
the count as `unconfirmed`) in the watchlist file; with no hit among those checked it says this is
not the whole list rather than "no chart closed above" (and the same when candidates were dropped
as too thin, which were not checked either). Every chart that could not
be read, in either stage, is counted, named (`COULD NOT READ: ...`) and stored with its reason in
`failed_symbols`; a chart with a missing bar in stage two is one of them. The summary line is the
US pack's: `Fetched X of Y charts (N failed or had no data); U passed the price / volume floor
(Rs 20) (F under it); K candidates, C confirmed with real highs and lows`, then `H of U charts
closed above their opening-range high by HH:MM:` (or `of the C checked` when some were not checked,
could not be read, or were too thin). A batch answer with no bar after the range at all cannot be
ruled out, so it goes to stage two rather than being dropped. Stage two waits `--pause` between its
live requests. When charts failed, the scan never says "nothing broke": with none read it says
it is a failed scan, not an empty one, and otherwise how many could not be read.

**The range is 09:15 plus `--or-minutes`, fixed by the clock.** In stage one a missing range bar
in the batch feed does not drop the symbol: the highest close of the range bars that are there is
still at or below the true high, so the test stays safe. (Anchored on the first bar instead, a
missing 09:15 close shifted the range late and lost INFY's real 09:30 break of 2026-10-16.) A hit
from such a symbol carries `stage1_range_partial` with the missing bar times; its own range is
always from the complete full chart.

**A chart with a missing bar is not a hit.** In stage two, every five-minute bar from 09:15 to the
scan time must be there. A missing bar is a stretch in which nothing traded: a halt, a stock locked
at its upper or lower circuit, or one too thin to trade every five minutes, and a close above the
range there is a price nobody could buy at. Stage one does NOT apply this: measured on 2026-10-22,
the batch feed dropped ITC's 13:10 and 13:15 bars although 99,035 and 70,544 shares traded in them,
so a gap in that feed is the feed's, and refusing on it lost a real breakout.

**The clock decides whether a scan can answer.** On a weekend or an NSE holiday, before the first
bar after the range has closed, after your `no_entries_after` (a break found then is expired), or
after the 15:30 close without `--at` to replay the day, `orb` says why in one line and writes
nothing. A live `--at` later than now is refused (the bar still forming would read as closed), and
so is one more than six days back. The cutoff is on the minute, as the US pack cuts it: a run at
15:00:40 is a 15:00 scan (the free feed keeps five days: replay with `--offline`). `--at`
is the cutoff: `HH:MM` cuts today's clock (with `--now`, that day's; offline with no `--now`, the
last session in the saved files, as the US pack replays its file's last day), or a date and a time,
`"2026-10-22 10:15"` IST. Offline, `--at` or `--now` is required. For `daily`, `--at` wants a date
and a time. `--or-minutes` must be a multiple of 5 from 5 to 120. A missing or damaged offline
folder (or a saved file that is not a batch answer), a `--config` that does not exist, and a bad
`--until` are each one plain line, exit 2; with `--json` every such line goes to stderr, so stdout
holds the JSON document or nothing. Ctrl-C is `scan.py: stopped`, exit 1, never a traceback. Hits come
earliest break first, each with its `bar_start`, `bar_index` and `setups`.

**`bar_index` counts from 1**: the 09:15 bar is bar 1, so SBIN's 10:10 break of 2026-10-22 is bar
12. `/nse-entry` and `/nse-exit` count the same way. The US pack counts from 0; subtract one to
compare.

**The watchlist is written on every run**, with no hits too, unless `--no-write`: a file written
only on a hit left the last run's hits for `/nse-entry` to re-check. `orb` writes `created_by`,
`created_at`, `mode` (`orb`), `as_of`, `universe`, `scanned`, `failed`, `failed_symbols`,
`unconfirmed`, `not_checked`, `source`, `or_minutes`, `interval`, `until`, `filters`, `note`,
`shortlisted`, `too_thin`, `liquidity_from`, `confirmed` (charts actually checked in stage two)
and `hits[]`. A hit has `symbol`, `setups`, `triggered`, `bar_start`, `bar_index`, `entry`,
`range_high`, `range_low`, `above_percent`, `volume_x`, `last`, and the same facts under the US
pack's names: `price`, `at` (when the breaking bar closed), `or_high`, `or_low`, `pct_over`.
`--json` prints the whole document with its `counts` (the US pack's `requested`, `scanned`,
`illiquid`, `unusable` (charts with a missing bar in stage two), `candidates`, `confirmed`,
`unconfirmed`, `hits`, and this scan's stage counts) and `seconds`. `scanned` is the charts that
came back less those under the price / volume floor (`illiquid`), so the two add up to `fetched`.
`created_at` is the clock (`--now`, or the real time), never the `--at` cutoff, which is `as_of`.

## Two things a zero volume can mean

The feed reports **zero volume for the 09:15 bar of every stock, every day** — while that bar's
price moves, and nothing moves a price but trades. The exchange's own daily total confirms it: the
bars add up to 2.7% to 8.3% less than the day's real volume. So that zero is **unknown**, not
nothing, and it is carried as unknown. A bar that is one price throughout on zero volume is the
other case: nobody traded, and it is never a breakout.

Counting the unknown 09:15 volume as zero had cut the opening-range average by a third and made
**every volume ratio half as large again as the truth** — SBIN's break of 2026-10-22 read 1.66x when
it was 1.10x. An unknown volume never clears a `--min-volume-x` floor; it is not confirmed.

`universe` writes the symbol list, **companies only**, one `SYMBOL|Name` a line (`--json` for the
count, `--offline` for a saved copy of the list: a folder holding `equities.json.gz`). The daily
bhavcopy carries about 2,706 symbols where the equity master list carries 2,359, and the difference is almost all exchange
traded funds — a fund is held rather than traded, so it sits near 100% delivery permanently and
would top a delivery screen every single day without ever being a trade.

The `daily` scan reads the price cache (`python nse-backtest/backtest.py --fetch 250` once; offline
after that, `--db FILE` for another cache, `--on DATE` for a past cached day). **With no cache, or
with `--live`, it scans the market's daily closes directly**: about three months of daily closes,
twenty companies a request (2,359 companies in 118 requests), and today's bar is dropped until the
close so a half-formed day is never judged. That covers the close-only rules (breakout, bull-flag,
reclaim-ma20, oversold, oversold-bounce); the delivery rules need the exchange's delivery figure,
which only the cache carries. With no `--setup`, every rule runs. `--live` filters: `--min-price`
(₹20) and `--min-volume` (1,000,000 shares on the last day); the cached scan keeps the replay's
100-lakh turnover floor instead, so a rule is screened on the universe it was measured on, and
says so if those flags are given. Offline with no list named, it scans the charts in the saved
files, not the whole universe. One close a day: a second close the feed sends for the same day
(the live tick) replaces the first rather than doubling it. It prints the US pack's lines:
`Fetched X of Y charts (N failed or had no data). L passed the liquidity filter (Rs 20+ and
1,000,000+ shares); S scanned.`,
then `H charts matched a setup: ...` with each rule in words, most rupees traded first. A scan that
could read no chart says it is a failed scan, not "No setups". Exit 0 when something fired, 1 when
nothing did (or the market is shut, for orb), 2 when there was nothing to scan or the input was
refused.

**Both daily scans write `nse/watchlist.json`** with `mode: "daily"` (unless `--no-write`), the
cached one with one hit per symbol and every rule that fired on it (`setups`), most traded first.
The cached scan says first which day's closes it read (`Daily scan on the price cache, closes of
DAY.`), prints `STALE: ...` when the cache is a whole session behind the clock (with no `--on`; the
watchlist file and `--json` then carry `stale`),
and ends with the count line: how many symbols traded that day and how many matched each rule.
`--symbols`, `--watchlist` and `--universe` limit the cached scan too; its `--json` prints the
watchlist document with `counts`, and each rule's candidates and record under `by_rule`. A day not
in the cache, or a file that is not a cache, is one plain line, exit 2.

Other flags: `--watchlist FILE` (a `watchlist.json`, a JSON list, or a text file, one symbol a
line, `SYMBOL|Name` allowed) and `--limit N`, `--or-minutes N` (the opening range, 15), `--window
N` (the rule's lookback), `--top N`, `--json`, `--save` (also `nse/scan.json`, for your notes),
`--now` (pin the IST clock). A missing or broken file, or an equity list that cannot be
fetched for `universe`, is one plain line, never a traceback.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/prices.sqlite` | read | written by `/nse-backtest --fetch` |
| `nse/scan.json` | written with `--save` (not with `--no-write`) | nobody; for your notes |
| `nse/universe.txt` | written by `universe` | `orb` and `daily --live` read it when no list is named |
| `nse/watchlist.json` | written by `orb` and both `daily` scans, every run unless `--no-write` | `/nse-entry`, `/nse-news`, `/nse-results` and `/nse-insider` read `hits[].symbol` |
| `config.json` or `nse/config.json` | read by `orb` | `no_entries_after` (default 15:00) |

## The rules, and why they live in one place

| Rule | Fires when |
|---|---|
| `delivery-spike` | delivery at least twice the stock's own median, on a rising close |
| `delivery-breakout` | delivery above its median and the close above the 20-day high |
| `accumulation` | delivery rising over the window while the price went nowhere |
| `breakout` | the close above the 20-day high, delivery ignored — the control |
| `bull-flag` | up more than 10% over the ten sessions before the last five, then the last five closes inside a 5% range |
| `reclaim-ma20` | closed under its 20-day average yesterday, above it today |
| `oversold` | Wilder's RSI 14 under 30 |
| `oversold-bounce` | RSI 14 under 30 yesterday, and a higher close today |

The RSI is the same arithmetic the US pack uses, and on 25 real symbols it gives the identical
number to the last decimal. The two RSI rules are here so `/nse-backtest` can measure them like
any other: "oversold" is a falling knife as often as a bargain, and only a record can say which.

The rules are defined once, in the shared `setups` module, and **both this skill and
`/nse-backtest` take them from there**. A rule screened on one definition and measured on another
is worse than no measurement. This skill used to load them out of `/nse-backtest`'s *folder*,
which broke the moment either skill was installed on its own.

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

**Every one of the eight shipped rules currently fails at least one of these.** The screener says it
outright:

```
None of these rules has a record worth acting on. A candidate list is not a reason to buy.
```

That is the honest state of them, not a bug.

## Example

```
$ python scan.py --all --top 3
Daily scan on the price cache, closes of 2026-10-23.

=== delivery-spike ===
   measured: 57 trades, 33.3% win rate, +0.78% average against a baseline of
   -1.89%, edge +2.67pp  (2026-07-13 to 2026-10-23)
   Only 57 trades measured, under 100: not enough to claim a win rate from.

   Nothing fired on 2026-10-23.

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

- **The daily scans see daily bars only**; `orb` sees five-minute bars, bar by bar, never tick
  by tick. The free chart and batch endpoints are unofficial and can lag: the first host, then the
  second on a 429 or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 2,300 of 2,359 charts (59 failed or had no data)".
  Never round a partial scan up to "the whole market".
- NSE holidays from the exchange's own list, with a saved copy for 2026; a year whose list is not
  known is said, never assumed open.
- **Rules are ordered by measured edge**, best first, so the worst sit at the bottom where they
  belong.
- **An illiquid stock is never a candidate**, the same 100 lakh turnover floor the replay uses.
- **`--on` only accepts a cached day** and refuses anything else rather than returning an empty
  list that looks like "nothing fired".
- A rule needs more cached days than its lookback window before it can fire at all.

## Steps

1. Once a week or so: `scan.py universe`.
2. `/nse-backtest --fetch 250` once, then measure each rule you care about.
3. Before the open: `--all` (or `daily --live` with no cache) to see what fired, with each rule's
   record. Tell the user the counts (fetched, liquid, scanned, hits) and the top few by setup.
4. **Read the header before the table.** If the rule lost money, the table is a curiosity.
5. After 09:35 IST (10:15 is the classic): `scan.py orb`. Tell the user how many usable charts
   broke their range and when, that a later hit is closer to the price now, and anything PARTIAL
   or COULD NOT READ.
6. The user picks one chart. Hand it on: `/nse-news SYM` (what is moving it), then `/nse-entry SYM`
   (the entry bar by bar); `/nse-delivery SYM` and `/nse-insider SYM` for more. Never pick for them.
7. `/nse-risk` to size it. That is the only skill that can say no.

## Rules

- Print the rule's record above its candidates, always.
- List the candidates even when the record is bad, and label the record.
- Order rules by measured edge, never alphabetically.
- Never present an unmeasured rule as though it had been checked.
- Say what was scanned: how many symbols, how many charts came back, how many were usable, and the
  time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure for a hit.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

- Never names a stock to buy or sell.
- Never hides a rule's losing record to make a list look better.
- Never invents a setup the replay cannot measure.
- Never places, sizes or checks an order, and never calls the data real time.

## Safety

A candidate list is not a reason to buy. Every rule shipped here currently fails at least one test
of whether its record means anything, and the screener says so. Nothing here is financial advice.
