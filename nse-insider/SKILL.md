---
name: nse-insider
description: >-
  Who Is Actually Buying. Reads NSE insider and promoter filings under the PIT
  regulations, bulk and block deals with the counterparty named, short-selling
  disclosures, and the daily foreign and domestic institutional flow. Separates desks
  turning their book from real positions. Watches for new filings since the last check
  and flags the ones in stocks you hold or watch. Use when asked whether insiders are
  buying, who bought a stock, what a promoter has been doing, what the institutions
  did, whether a volume spike was one party, or to alert on new insider filings.
argument-hint: "[--insiders --symbol SYM | --bulk | --block | --short | --flows] [--new] [--member NAME] [--days N]"
---

# Who Is Actually Buying

Three disclosures the Indian exchange publishes, and the US has no equal of the first two.

| Disclosure | What it gives you |
|---|---|
| **Insider / PIT filings** | the person, the mode, buy or sell, the security, the shares and the value, the trade date and the filing date, and a link to the filing. The category (promoter, director, relative) only where the filing gives one: 170 of INFY's 197 rows leave it as `-` |
| **Bulk and block deals** | the client's name and the price they paid, for anything over 0.5% of a company in a day (bulk) or one negotiated trade (block) |
| **Short selling** | the stock and the quantity sold short that day; no client, no side, no price |
| **FII / DII flows** | what foreign and domestic institutions bought and sold across the market |

Congressional disclosures in the US can arrive 45 days after the trade. SEBI gives an insider two
trading days to tell the company and the company two more to tell the exchange, so most filings
here arrive within days, and they name the mode, so you can tell a promoter buying in the market
from an employee trust settling ESOPs. Not all of them: the clock starts only once a quarter's
trades reach Rs 10 lakh, and INFY's filing of 5 February 2026 carries a trade of 5 January.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python "${CLAUDE_SKILL_DIR}/insider.py" --insiders --symbol INFY --days 365  # PIT filings
python "${CLAUDE_SKILL_DIR}/insider.py" --insiders --new                     # new filings in your stocks
python "${CLAUDE_SKILL_DIR}/insider.py" --insiders --symbol INFY --member mitrankur --year 2026
python "${CLAUDE_SKILL_DIR}/insider.py" --insiders --tickers INFY TCS --type "Market Purchase"
python "${CLAUDE_SKILL_DIR}/insider.py" --block --days 10                    # block deals
python "${CLAUDE_SKILL_DIR}/insider.py" --bulk --days 10 --real-only         # bulk, churn removed
python "${CLAUDE_SKILL_DIR}/insider.py" --bulk --new --tickers SBIN ITC      # new deals, yours flagged
python "${CLAUDE_SKILL_DIR}/insider.py" --short --symbol ADANIENSOL          # short selling
python "${CLAUDE_SKILL_DIR}/insider.py" --flows                              # institutional flow
python "${CLAUDE_SKILL_DIR}/insider.py" --bulk --block --flows               # one section each
```

`--new` shows only what was not shown at the last check (the first check shows the whole window).
It remembers only the rows it actually put in front of you: a row cut off by `--top` or hidden by
`--real-only` stays new for the next check, and the output says how many were cut off. `--dry-run`
leaves the state alone. `--member NAME` (or `--who`) follows one person or client by the words of
the name in any order (`mitrankur`, `Mitrankur Majumdar` and `Majumdar, Mitrankur` all find him),
and repeats. `--type MODE` (or `--mode`) keeps one insider mode, such as `ESOP` or
`Market Purchase`; a blank `--member ''` or `--type ''` is no filter. `--since DATE`, `--year YYYY`
and `--today DATE` set the window, and the header names it by its dates; `--days` and `--top` must
be 1 or more. `--min-crore N` (0 or more) ignores anything smaller; `--top N` (or `--limit`) limits
rows per section. The header says how many were found and, when a filter is used, how many matched;
a filter that does not apply to a feed (`--type` on deals, `--member` on short selling or flows,
`--real-only` anywhere but bulk and block deals) is said to be unused rather than counted. Several
feeds can be asked for at once and each gets its own section; with `--json` one feed is a list and
several are one object keyed by feed, and every line on stderr names the section it is about.
`--save` writes every row to `nse/insider.json` and says so on stderr, so `--json --save` stays
valid JSON. `--offline DIR` reads saved answers (`insider-SYM.json`, `bulk_deals.csv` or
`bulk_deals.json`, the same for `block_deals`, `short_selling.json`, and `fii-dii.json`).

A symbol is capitals, digits, `&` and `-`, as every one of the exchange's 2,359 listed equities is.
`INFY.NS` is refused: the exchange would answer it with an empty list, which reads as "no filings".
For the same reason an empty answer says the spelling may be wrong.

To be alerted, run `--new` on a loop (`/loop 1h`) or a scheduled task: each filing and deal alerts
once, and exit 1 means nothing new.

Exit codes: **0** something was shown; **1** nothing was shown, or with `--new` nothing is new;
**2** the question was refused (no feed asked for, `--insiders` with no symbol, a symbol that is not
an NSE symbol, a bad date, year or number, a state file that cannot be read) or nothing asked for
could be read, with the reason. When only some companies or feeds could not be read they are named
and the rest is reported, and if nothing else was shown the exit is 2, never 1: a company that
could not be read is never reported as "nothing new".

## What it reads and writes

| File | Direction | What |
|---|---|---|
| `nse/portfolio.json` (`positions[].symbol`, or `tradingsymbol` or `ticker`), `nse/plan.json` (`symbol`), `nse/watchlist.json` (`hits[].symbol`) | read | the stocks that are yours: their rows are flagged `<- yours`, and `--insiders --new` with no symbol watches them. A file that exists but cannot be read is named, not skipped silently, and so is a name in them that is not an NSE symbol |
| `nse/state/insider.json` | read and written by `--new` | what has been shown, per feed, so each filing alerts once, and when each feed was last checked. Written whole or not at all. A broken one is refused, never read as empty (that would show everything again as new) |
| `nse/insider.json` | written with `--save` | for your notes; no other skill reads it |

Asking about a company with `--symbol` does not make it yours, in either feed. For deals, which
are market-wide, the `--tickers` you name are flagged as well as your files.

## The trap under everything: the deals answer stops at 70 rows

The exchange's JSON answer for bulk, block and short-selling deals carries **at most 70 rows**, with
no page that gives the rest. An average day has about 110 bulk deals: in 247 days of complete data
(the CSV export, stored by the separate insider-activity agent), 206 days had more than 70. Read
the JSON and a busy day silently loses a third of its deals, and a 30-day window comes back as the
first 70 rows of its first day: the saved answer here asked for 30 days and holds only 24 September.

So bulk and block deals are read from the exchange's **CSV export**, which returns the whole range.
Short selling is still read as JSON (its CSV columns were never checked), and any answer of 70 rows
read as JSON, live or saved, is reported as "At least 70 disclosed" with the reason, never as a
complete count.

## The trap that matters most: most bulk deals are not positions

In the real 24 September 2026 answer, **50 of the 70 bulk deals are a client on both sides of the
same stock on the same day.** A desk passing stock through itself crosses the 0.5% disclosure
threshold on both legs, so it appears twice. MICROCURVES bought and sold 313,241 GNA that day, at
630.87 and 631.41: inventory going round, not anyone taking a view. `--real-only` leaves 20 of 70.

**Block deals are mostly the opposite.** Of the 70 block deals saved (24 September to 1 October),
3 are two-sided (HDFC LIFE on both sides of SEDEMAC); the rest are one-sided transfers between named
parties, such as HOECHST GMBH selling 3,500,000 SANOFI at 3,052 (1,068.2 crore) to SANOFI
HEALTHCARE INDIA, or INFINITE TRADE AND INVESTMENT selling 8,600,000 ADANIENT at 2,905
(2,498.3 crore). If you only look at one of these two feeds, look at block deals.

Being on both sides at all is the signal, not how square the legs are: a 10% band would let LAXMI
TRADE SOLUTIONS through on HEROMOTORS that day, 2,226,406 bought against 2,552,391 sold, 13% apart.
The match is on the client's exact name, so related parties under two names are not caught:
PABRAI WAGONS FUND sold 14,572,000 EDELWEISS to PABRAI WAGONS ETF on 1 October, and both legs show
as one-sided. Read the names.

## The other traps

- **Insider filings need a symbol.** Asked market-wide the endpoint answers 200 with an empty list
  however wide the dates. That reads as "no insider activity anywhere", which is wrong: INFY returns
  197 filings over a year. The script refuses the market-wide call rather than returning nothing.
  The symbol is URL-encoded, so `M&M` is asked for as M&M, not as a company called M.
- **Read the mode column.** `ESOP` is an employee exercising options and happens constantly.
  `Market Purchase` is somebody choosing to buy. They are not the same signal and nothing here
  ranks them for you. `Off Market` is a transfer outside the exchange (15 of INFY's, mostly the
  employee trust passing shares on) and is not counted with the open-market modes.
- **Read the side as well.** The mode does not say buy or sell: 72 of INFY's ESOP rows are sales
  of ESOP shares. `side` is the filing's own transaction type; three INFY rows leave that blank, and
  for those it is read from the holding before and after (`Nil` meaning none). Where neither says,
  it is `unknown`, never a guess; so is a row whose type and holding disagree (one of them is
  wrong and a row cannot say which), and one whose type is neither a buy nor a sale, such as a
  pledge, where what moves may be the shares pledged. The JSON's `side_from` gives the reason.
- **Futures and options are not shares.** When a filing names a derivative, a `derivative` column
  appears, and the JSON carries the filing's buy and sell notional value and units as filed
  (`derivative_buy_value` and the rest), unscaled: no real derivative filing has been seen to
  check their unit against.
- **`secVal` is in rupees**, shown here in crore (`cr`). A 0.263 crore ESOP row is not a promoter
  bet. `shares` is the filing's quantity; the JSON also carries the holding before and after.
- **A short-selling row has no value.** It names no price, so its value is blank, never zero, and
  `--min-crore` does not apply to it.
- **FII/DII flow is market-wide.** It says nothing about any one stock, and a day of heavy foreign
  selling routinely coincides with individual stocks being bought hard. The exchange serves only
  its latest day; a flow dated outside the window (`--today` pinned earlier) is not shown as current.
- **Filing date and trade date differ.** Both are shown: `filed` and `traded` (`from to to` when a
  filing covers several days), with the security type. A filing that arrives five days after the
  transaction is still news on the day it arrives, but the price moved earlier. Every insider row
  is listed after the table with its transaction id, filing id and the exchange's link.
- **One filing can carry many trades.** INFY's 197 rows over a year are 27 filings, one of them
  102 trades, so each row is keyed on its own transaction id; keying on the filing would have
  alerted on one trade and silently marked the rest as seen.
- **A deal has no page of its own.** An insider row links its own filing; a deal or a flow names
  the exchange's report page it is listed on.
- **Nothing is set aside silently.** A row with no readable date, symbol or quantity, or one naming
  a different company from the one asked about, is counted and said, not dropped.

## How long it took to disclose, and why nothing is called late

Each insider row shows `days` (calendar days from the last day of the trade, `acqtoDt`, to the
exchange), `co` (trading days from that day to the company being told) and `nse` (trading days
from the company being told to the exchange publishing it), counted on the exchange's own holiday
list. Where that year's list is not known (the exchange publishes only the current year's), the
trading-day counts are blank rather than counted as if there were no holidays. A filing whose
dates contradict each other (published before the trade) gets no delay at all.

SEBI's PIT Regulation 7(2) gives each step two trading days, but **the clock starts when a person's
trades in a calendar quarter first add up to Rs 10 lakh**, and a single row cannot show when that
was. Measured: an INFY filing published 5 February 2026 carries a trade of 5 January and one of 2
February; the January trade looks a month late and almost certainly was not. So the delays are
shown as measured and no row is called late. The US pack can flag a late congressional report
because that deadline is absolute; this one is not.

## Steps

1. `--block --days 10` for the genuine institutional transfers.
2. `--insiders --symbol SYM --days 365` for anyone inside the company.
3. Check the mode and side columns before concluding anything about intent.
4. Cross-check with `/nse-delivery --symbol SYM` — a block deal should show up as a delivery spike on the
   same date. If it does not, the two disclosures disagree and one of them needs explaining.
5. `/nse-news SYM` for an announcement on the same date.
6. `/nse-risk` to size anything, which is the only skill that can say no.

## Example

On the saved answers (the bulk JSON of 24 September 2026, which shows the 70-row cap):

```
$ python insider.py --bulk --days 10 --real-only --top 3 --today 2026-10-02

Bulk deals, last 10 days (2026-09-22 to 2026-10-02). At least 70 disclosed, 50 two-sided hidden, 20 shown.
The exchange's JSON answer stops at 70 rows and this one has 70, so more were probably disclosed than are listed here, and the latest days may be the ones missing.

date         symbol        side   who                             shares       price      cr
-----------  ------------  -----  ------------------------------  -----------  ---------  -------
2026-09-24   FIRSTCRY      SELL   NEWQUEST ASIA INVESTMENTS I...  11535577     175.15     202.05
2026-09-24   FIRSTCRY      BUY    GOLDMAN SACHS INVESTMENTS M...  6800000      175.0      119.0
2026-09-24   HEROMOTORS    BUY    NEO APEX SHARE BROKING SERV...  2550010      118.03     30.1

A client on both sides of the same stock on the same day is turning stock, not building a position. --real-only drops them. Public disclosures, read fast; not inside information. Listed at https://www.nseindia.com/report-detail/display-bulk-and-block-deals

17 more not shown (--top 3).
```

(The `note` and `yours` columns, empty here, are trimmed from the right.)

## Rules

- Name the counterparty. An anonymous figure is not this skill's output.
- Label a two-sided deal as two-sided rather than dropping it silently. The deal happened.
- Show both the filing date and the trade date.
- Never score or rank a mode. `ESOP` against `Market Purchase` is a judgement for the reader.
- Never call a capped answer complete, or an unread company quiet.

## What it never does

- Never names a stock to buy or sell.
- Never treats an insider purchase as a recommendation. Insiders are wrong often.
- Never places, prepares or sizes an order.
- Never reports a market-wide insider query as "no activity".
- Never calls a filing late, or says anyone had inside information: these are public disclosures,
  read fast.

## Safety

A disclosure says what somebody did, not whether they were right. Insiders and institutions lose
money. Nothing here is financial advice and no part of this skill can place a trade.
