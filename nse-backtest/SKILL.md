---
name: nse-backtest
description: >-
  Replay. Runs a setup over the market's own history and reports what it actually
  did, against the only number that matters: what buying everything would have done
  over the same days. Caches daily bars locally so a replay is offline. Use when
  asked whether a rule works, to test an idea before trading it, or to check a
  claimed edge.
argument-hint: "[--fetch DAYS] [--setup delivery-spike|delivery-breakout|accumulation|breakout|oversold|oversold-bounce] [--hold N] [--stop PCT]"
---

# Replay

The only skill here that can tell you a rule is worthless.

Everything else reports what the exchange published. This one takes a rule, runs it over every
stock and every day it has, and prints the numbers — including the one most backtests leave out:
**what buying anything at random would have done over the same days.**

A setup that wins 55% of the time is a bad setup if the market rose and everything won 60%. That
comparison is printed every time, because leaving it out is how a rule gets flattered into looking
like an edge.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python "${CLAUDE_SKILL_DIR}/backtest.py" --fetch 250                       # cache a year, once
python "${CLAUDE_SKILL_DIR}/backtest.py" --setup delivery-spike            # replay it
python "${CLAUDE_SKILL_DIR}/backtest.py" --setup breakout --hold 5 --stop 3
python "${CLAUDE_SKILL_DIR}/backtest.py" --setup delivery-spike --trades   # list every trade
```

`--hold N` trading days (default 10), `--stop PCT` below entry (default 5), `--target PCT`
optional, `--window N` the lookback a rule uses (default 20), `--db FILE` a price cache other than
`nse/prices.sqlite`, `--json` machine readable.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/prices.sqlite` | written by `--fetch`, read by every replay | `nse-scan` can read the same cache |
| nothing | read from other skills | this skill stands alone |

One request gets one trading day for **every** stock, which is the right shape: 250 requests buys a
year of the whole market. Days already cached are never re-fetched, and holidays are remembered so
they are not asked about twice. 75 days is about 25 MB.

## The setups

| Name | Fires when |
|---|---|
| `delivery-spike` | delivery is twice the stock's own 20-day median, on a rising close |
| `delivery-breakout` | close above the window's high **and** delivery above its median |
| `accumulation` | delivery up more than 25% over the window while price moved under 5% |
| `breakout` | close above the window's high. **The control** — it ignores delivery entirely |
| `oversold` | Wilder's RSI 14 under 30 — the same arithmetic as the US pack, identical on real data |
| `oversold-bounce` | RSI 14 under 30 yesterday, and a higher close today |

`breakout` is there on purpose. If a delivery setup does not beat it, the delivery number is adding
nothing and you have learned something more useful than a win rate.

The rules live in the shared `setups` module, which `/nse-scan` reads too, so a rule is always
screened on exactly the definition it was measured on.

## What it measured, 2026-07-13 to 2026-10-23

75 trading days, 2,226 symbols, holding 10 days with a 5% stop. Re-measured on 2026-10-24:

| Setup | Trades | Win rate | Average | Edge |
|---|---|---|---|---|
| `delivery-spike` | 57 | 33.3% | **+0.78%** | **+2.67pp** |
| `breakout` | 5,235 | 26.9% | −1.36% | +0.53pp |
| `delivery-breakout` | 1,495 | 28.0% | −1.38% | +0.51pp |
| `accumulation` | 2,158 | 29.7% | −1.55% | +0.34pp |
| `oversold` | 6,164 | 25.8% | −1.92% | **−0.03pp** |
| `oversold-bounce` | 2,637 | 23.2% | −2.28% | **−0.39pp** |

Baseline over the same days: **31.6% win rate, −1.89% average.** The market fell, so everything lost
money; most of these rules just lost less.

**The two RSI rules lost to buying anything at all.** Buying Indian stocks because they looked
oversold did slightly worse than buying at random over these 75 days, and buying the first up day
afterwards did worse still. That is the result, not a reason to drop the rules: it is exactly what
a record is for.

An earlier version of this table showed `accumulation` at −1.15% and +0.74pp and `delivery-breakout`
at −1.24% and +0.65pp. Those were stale — measured before the fix that fills a gap through the stop
at the open — and they flattered both rules. The figures above are a fresh run.

**Read this honestly.** `delivery-spike` is the only one that made money, and it fired 57 times in
75 days. Fifty-seven trades is not evidence of anything — the skill says so in its own output. The
median return of every setup is **exactly the stop**, which means more than half of all trades get
stopped out and the averages are carried by a few +63% and +95% outliers. That is a fragile shape,
not an edge.

## The assumption that makes backtests lie

**A gap through the stop fills at the open, not at the stop.**

The first version of this replay filled every stop at the stop price. Every setup then reported a
worst trade of exactly −5.29%, the stop plus costs — which cannot happen. A stock that closes at
100 and opens at 88 does not fill your 95 stop at 95.

Fixing it changed the worst `breakout` trade from **−5.29% to −89.86%** and found 103 trades that
had gapped straight through. Overnight gaps are where real losses live.

Three more rules the replay follows:

- **Entry is the next day's open**, never the signal day's close. A rule that buys the close of the
  day it fired has already seen the answer.
- **The stop is checked before the target.** Within one daily bar there is no way to know which came
  first, so the pessimistic order is the honest one.
- **Costs come off both the setup and the baseline**, at 0.293% round trip. Charging the rule for
  trading and not the comparison would be a free 0.293pp of fake edge.

## Data and limits

- **Daily bars only.** No intraday, so this cannot test anything that depends on the opening range
  or on timing inside a session.
- **One window is not a strategy.** These numbers come from one three-month stretch of a falling
  market. A rule that works here may be a rule that likes falling markets.
- **No slippage beyond gaps, no liquidity check at the exit.** A 5,000-trade result assumes every
  fill was available. On an illiquid stock it was not.
- **Survivorship is not handled.** Delisted stocks are absent from today's master list, so a rule
  is tested only on companies that still exist.
- Requires at least `window + hold + 2` cached days before it will run.

## Steps

1. `--fetch 250` once. It takes a few minutes and costs nothing afterwards.
2. Replay your rule and the `breakout` control side by side.
3. Look at the **edge** line, not the win rate.
4. Look at the median. If it equals the stop, the stop is doing the work.
5. If trades are under about 30, cache more history before believing anything.
6. Only then hand a live candidate to `/nse-risk` for sizing.

## Rules

- Always print the baseline. A result without it is not a result.
- Always print the trade count, and say when it is too small.
- Fill a gapped stop at the open. Never at the stop.
- Never report a positive average without the median beside it.

## What it never does

- Never says a setup will work. It says what it did, on these days.
- Never optimises a parameter until it looks good. Fitting a rule to one window is how a backtest
  becomes a story.
- Never places or sizes a trade.
- Never hides a losing result.

## Safety

A backtest is a simulation, not a prediction. It is run on past prices with hindsight about which
companies still exist, and the real thing has slippage, partial fills and gaps this cannot see.
Nothing here is financial advice.
