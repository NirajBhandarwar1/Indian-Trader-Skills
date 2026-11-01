---
name: nse-insider
description: >-
  Who Is Actually Buying. Reads NSE insider and promoter filings under the PIT
  regulations, bulk and block deals with the counterparty named, and the daily
  foreign and domestic institutional flow. Separates desks turning their book from
  real positions. Use when asked whether insiders are buying, who bought a stock,
  what the institutions did, or whether a volume spike was one party.
argument-hint: "[--insiders --symbol SYM | --bulk | --block | --short | --flows] [--days N] [--real-only]"
---

# Who Is Actually Buying

Three disclosures the Indian exchange publishes, and the US has no equal of the first two.

| Disclosure | What it gives you |
|---|---|
| **Insider / PIT filings** | the person, their category, the mode and the value — filed within **two trading days** of the transaction |
| **Bulk and block deals** | the client's name and the price they paid, for anything over 0.5% of a company in a day |
| **FII / DII flows** | what foreign and domestic institutions bought and sold across the market |

Congressional disclosures in the US arrive up to 45 days late. Indian insider filings arrive in two
days, and they name the mode — so you can tell a promoter buying in the market from an employee
trust settling ESOPs.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python ${CLAUDE_SKILL_DIR}/insider.py --insiders --symbol INFY --days 365  # PIT filings
python ${CLAUDE_SKILL_DIR}/insider.py --block --days 10                    # block deals
python ${CLAUDE_SKILL_DIR}/insider.py --bulk --days 10 --real-only         # bulk, churn removed
python ${CLAUDE_SKILL_DIR}/insider.py --flows                              # institutional flow
```

`--min-crore N` ignores anything smaller, `--top N` limits rows, `--json` is machine readable,
`--save` writes `nse/insider.json`. Exit 0 on success, 1 if a mode needs a symbol, 2 if the exchange
gave nothing.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/insider.json` | written with `--save` | `nse-delivery` to explain a spike, `nse-scan` to rank |
| nothing | read | this skill runs on its own |

## The trap that matters most: 89% of bulk deals are not positions

Measured over 10 days to 2026-10-23: **70 bulk deals disclosed, 62 of them the same client on both
sides of the same stock on the same day.** A desk passing stock through itself crosses the 0.5%
disclosure threshold on both legs, so it appears twice, at the top, with the largest numbers.

MICROCURVES sold 2,099,164 CMLL at 633.54 and bought 2,099,164 at 633.26 — ₹133 crore each way, flat
by the close. That is inventory going round, not anyone taking a view.

Read bulk deals without this filter and you will conclude that smart money is piling into whatever
the brokers happened to be working that week. `--real-only` leaves **8 of 70**.

**Block deals are the opposite.** Over the same window, **16 of 16 were genuinely one-sided**: large,
negotiated, one price, two different counterparties. GALLANTT at ₹149.84 crore between two named
parties, ADANIENSOL at ₹660.8 crore to PI Opportunities AIF. If you only look at one of these two
feeds, look at block deals.

An earlier version only flagged legs that were flat within 10%, and that let IRAGE through on CMLL —
₹33.70 crore bought against ₹28.73 crore sold the same day, 15% apart, which read as a real position
and is a desk working an order. **Both sides at all is the signal, not how square the legs are.**

## The other traps

- **Insider filings need a symbol.** Asked market-wide the endpoint answers 200 with an empty list
  however wide the dates. That reads as "no insider activity anywhere", which is wrong: INFY returns
  197 filings over a year. The script refuses the market-wide call rather than returning nothing.
- **Read the mode column.** `ESOP` is an employee exercising options and happens constantly.
  `Market Purchase` is somebody choosing to buy. They are not the same signal and nothing here
  ranks them for you.
- **`secVal` is in rupees**, shown here in crore. A 0.263 crore ESOP row is not a promoter bet.
- **FII/DII flow is market-wide.** It says nothing about any one stock, and a day of heavy foreign
  selling routinely coincides with individual stocks being bought hard.
- **Filing date and trade date differ.** Both are shown: `filed` and `traded`. A filing that arrives
  five days after the transaction is still news on the day it arrives, but the price moved earlier.

## Steps

1. `--block --days 10` for the genuine institutional transfers.
2. `--insiders --symbol SYM --days 365` for anyone inside the company.
3. Check the mode column before concluding anything about intent.
4. Cross-check with `/nse-delivery SYM` — a block deal should show up as a delivery spike on the
   same date. If it does not, the two disclosures disagree and one of them needs explaining.
5. `/nse-news SYM` for an announcement on the same date.
6. `/nse-risk` to size anything, which is the only skill that can say no.

## Example

```
$ python insider.py --bulk --days 10 --real-only --top 3

Bulk deals, last 10 days. 8 disclosed, 62 two-sided hidden.

date         symbol      side   who                              cr
-----------  ----------  -----  -------------------------------  ------
2026-10-13   AQYLON      SELL   LEADING LEASING FINANCE & INV..  18.24
2026-10-13   AQYLON      BUY    ABHISHEK ASHVINBHAI KAMDAR HUF   16.50
2026-10-13   ARFIN       SELL   BHOLA MOTOR FINANCE PRIVATE L..   9.27
```

## Rules

- Name the counterparty. An anonymous figure is not this skill's output.
- Label a two-sided deal as two-sided rather than dropping it silently. The deal happened.
- Show both the filing date and the trade date.
- Never score or rank a mode. `ESOP` against `Market Purchase` is a judgement for the reader.

## What it never does

- Never names a stock to buy or sell.
- Never treats an insider purchase as a recommendation. Insiders are wrong often.
- Never places, prepares or sizes an order.
- Never reports a market-wide insider query as "no activity".

## Safety

A disclosure says what somebody did, not whether they were right. Insiders and institutions lose
money. Nothing here is financial advice and no part of this skill can place a trade.
