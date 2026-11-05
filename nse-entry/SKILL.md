---
name: nse-entry
description: >-
  Entry Timer for NSE stocks. Watches a setup bar by bar and names the moment to
  get in. Reads free keyless 1-, 5- or 15-minute bars, builds the opening range
  (NSE opens 09:15, so the first 15 minutes end at 09:30) and calls the entry on
  the first completed bar that CLOSES above the range high, or above a level you
  give it, with an optional volume check. Says waiting and how far, triggered
  with the time and price, expired, or not open yet, and writes nse/entry.json
  for /nse-exit. Long side only. It never places an order. Use when the user says
  "tell me when to get in", "watch SBIN", "has it broken out", "did it break the
  opening range", "entry signal", "time my entry", "is the market open", or after
  /nse-scan and /nse-news, before /nse-exit.
argument-hint: "check SYMBOL [--setup orb|level] [--interval 5m|1m|15m]"
---

# nse-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. A price that pokes above
the high in the middle of a bar is not an entry. A bar that CLOSES above it is. Then it says the
time and the price, writes them down for the Exit Planner, and stops.

It never buys anything. Day trading loses most people money, and a clean signal does not change
that.

Scripts live in `${CLAUDE_SKILL_DIR}`.

```bash
python "${CLAUDE_SKILL_DIR}/entry.py" clock                            # is the NSE open
python "${CLAUDE_SKILL_DIR}/entry.py" check SBIN                       # 15-minute opening range, 5m bars
python "${CLAUDE_SKILL_DIR}/entry.py" check SBIN --write               # also save nse/entry.json
python "${CLAUDE_SKILL_DIR}/entry.py" check SBIN --setup level --level 971.20
python "${CLAUDE_SKILL_DIR}/entry.py" check SBIN --interval 1m --min-volume-x 1.5
python "${CLAUDE_SKILL_DIR}/entry.py" watch SBIN --until 15:00         # one line per closed bar
python "${CLAUDE_SKILL_DIR}/entry.py" watchlist                        # every hit in nse/watchlist.json
```

Other flags: `--interval 1m|5m|15m` (5m), `--or-minutes N` (the opening range, 15; `watchlist` uses the scan's own `or_minutes` unless this is given), `--side` (buy; a sell or short is refused with the reason: this pack plans long trades), `--json`, `--every SECONDS` (watch: how often to poll while the free feed is behind the clock, 30), `--out FILE` (write the trigger there instead of `nse/entry.json`), `--watchlist FILE`, `--config FILE` (a named config that does not exist is refused, not replaced by defaults).

`watch` wakes just after each bar CLOSES — on NSE's grid, which runs from 09:15, not from the hour — then polls every `--every` seconds for up to a minute until the feed has that bar. While a closed bar is held back because the feed's last trade is before its close, it asks again every `--every` seconds, so a trigger on that bar is reported within the settle minute, not a bar late.

**How far back.** A `--now` replay without `--offline` asks the feed for 5 days, 1 month or 60 days as the date needs. The free feed keeps 1-minute bars for about a week and intraday bars for about 60 days; a date beyond that is refused with the limit named.

**A bar nobody traded is never an entry.** A bar that is one price throughout on zero volume is the feed carrying the last price forward. The 09:15 bar is different: it reports zero volume every day while its price moves, so its volume is *unknown*, and it is left out of the opening range's average volume rather than counted as zero. Counting it had made every `volume_x` half as large again as the truth.

`--now "2026-10-22 10:15"` pins the clock (IST) to replay a day. `--offline` reads saved chart
JSON — **a file, or a folder**, which is what lets `watchlist` replay every symbol from one flag.
With either, `watch` replays bar by bar without waiting.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/watchlist.json` | read | written by `nse-scan` |
| `config.json` | read | `no_entries_before`, `no_entries_after` |
| `nse/entry.json` | written with `--write` | `nse-exit` reads it |

Exit 0 when it triggered, 1 when it did not, 2 when the request was refused.

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes. **NSE opens 09:15**, so that
  is the 09:15, 09:20 and 09:25 five-minute bars, and the range ends 09:30. The level to beat is
  the range high. The range is anchored to 09:15 by the clock, never to the first bar the feed
  sent. If the feed never sent a bar inside it, the range is built from the bars there are once
  the clock has passed its end, and the output says so: "built from 2 of 3 bars: the feed sent no
  09:25 bar". A range bar the feed is still catching up to keeps the range forming.
- **Level.** A price you give, such as a 20-day high from `/nse-scan`.
- **The trigger** is the first completed bar after the range that CLOSES above the level. A wick
  above is not a trigger. A close exactly at the level is not above it.
- **The time** is that bar's close time: the 10:10 bar closes at 10:15.
- **Only closed bars count.** The feed's last bar is the one forming right now, and its high is
  whatever the price has touched so far. It is held out until the clock passes its end.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger bar's volume at 1.5
  times the opening range bars' average known volume, for a level setup as for an orb. That
  average is fixed once the range has closed, so a later check never changes an earlier verdict.
- **Time limits.** No entries before 09:30 or after 15:00 (both in `config.json`). The afternoon
  cutoff leaves room before the intraday square-off.

## How it decides, as the US pack does

- **A bar counts once the feed has caught up to it.** On live data the newest bar whose close time
  has passed still waits while the feed's last trade is before that close; it counts once the feed
  catches up, a later bar exists, or a minute has passed, and the output says it is waiting.
- **A late trigger says it is late.** "That was 45 minutes ago; the last close is Rs 965.05. A late
  entry is a different trade."
- **A close above that did not trigger says why**: on volume under your `--min-volume-x` floor, or
  before `no_entries_before`.
- **A close at the cutoff still counts**; one after it does not. A bar closing at or before the
  cutoff that is only waiting for the feed to catch up is not expired: it can still trigger.
- **A trigger on a bar the feed has not fully caught up to says so**: "the close can still move a
  tick or two". A fresh trigger says "This is the moment to get in, if you take the trade".
- **watch** prints the pre-open line once, then one line per closed bar (the range forming, then
  each bar against the level; joining mid-session it prints the range and the latest bar only),
  writes `nse/entry.json` (or `--out`) the moment it triggers, and stops after five failed reads in
  a row, on expiry, at the close or at `--until`.
- **watchlist** checks every hit and, with `--setup level`, uses each hit's own `level` from the
  watchlist, and the scan's `or_minutes` unless `--or-minutes` is given; the table shows the time,
  price, level, last close and distance, a symbol that could not be read with the reason, then
  "N of M triggered". Its `--offline` must be a folder: a single file holds one stock and is
  refused.
- **An offline file with no `--now` is read as of its own time**, the feed's last trade in it.
- **Refused** (exit 2): `--level` with an orb, `--setup level` without `--level` (check, watch),
  a level or volume floor of 0 or less, an `--or-minutes` that is not whole bars, a short, a
  `no_entries_before`/`no_entries_after` in the config or an `--until` that is not a time like
  15:00 (never silently replaced by the default), a replay date older than the feed keeps. A replay date in a year whose holiday list is not known is said, not
  guessed.
- `check --write [PATH]` saves the result whatever its status (`/nse-exit` plans only a triggered
  one). The file carries `created_by`, `side`, `checked_at`, `source`, `reason` (always, whatever
  the status), the trigger's
  `time`, `price` and `bar_index` (its place on the clock from 09:15, counting from 1: the
  10:10 bar is bar 12 even if the feed left a bar out), and the opening range with its `minutes`.

## The Indian session

| | |
|---|---|
| Pre-open auction | 09:00–09:15, matched at the open — not continuous trading, so no entry is timed in it |
| Regular session | **09:15–15:30 IST** |
| Opening range | 09:15–09:30 |
| Intraday square-off | ~15:20 at most brokers (a **broker convention, not an exchange rule**) |

**IST never changes.** India has no daylight saving, so the clock needs no timezone files and
ignores the computer's own zone.

**Holidays come from the exchange**, not from a list written into the code. The NSE closes about
sixteen times a year and several of those move with the lunar calendar, so they are fetched live.
A day marked with a star is **Muhurat trading**: the market is closed and then opens for about an
hour in the evening for a ceremonial session, whose timing the exchange announces separately. This
skill reports such a day as closed rather than guessing the hour.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after `/nse-scan`. Check `/nse-news`
   for it first.
2. Run `entry.py check SYMBOL`. Say the status in one line: waiting and how far, triggered at what
   time and price, or expired.
3. To wait for it, run `entry.py watch SYMBOL --until 15:00` in the background, or `check --write`
   on a `/loop` every 5 minutes.
4. When it triggers, say the time and the price, **and that nothing was placed.** If the trigger
   is more than a bar old, say that too: a late entry is a different trade.
5. Hand off to `/nse-exit` to set the stop and target BEFORE anything is bought.

## Rules

- Bar by bar, on free bars that are unofficial and can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise.
- Long side only.
- One trigger per setup per day: the first close above. Later closes do not reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that is `/nse-exit`), never
moves the level after the fact, and never counts a bar that has not closed.

## Safety

Education and tooling, not financial advice. Only /nse-broker can lead to an order, after /nse-risk and your yes. A stock can hit
its upper or lower **circuit** and stop trading altogether, which no signal predicts.
