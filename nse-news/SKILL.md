---
name: nse-news
description: >-
  News Monitor for NSE stocks. Tracks what breaks on every stock you watch or hold:
  the exchange's corporate announcements (India's 8-K: results, resignations,
  orders, defaults, regulatory actions), headlines that name each company, and how
  far each stock is from its previous close. Shows only what is new since the last
  check, newest first, in IST, tags each item, marks the ones that matter with "!",
  and never repeats one. Use when the user says "any news on TCS", "why is this
  moving", "what's the news on my watchlist", "morning briefing", "did anything
  break", "what was filed today", or after /nse-scan finds setups.
argument-hint: "[SYM ...] [--hours N] [--all] [--market]"
---

# nse-news

A chart can look perfect and still be about to gap on a result, a resignation or a default. Before
a setup becomes a trade, this skill reads the two feeds that carry those first, for every stock on
the watchlist, and tells you only what is new. It prints how far each stock has moved, so a filing
and a move sit side by side.

**No key, no token, no account.** Standard library Python.

```bash
python "${CLAUDE_SKILL_DIR}/news.py"                                 # watchlist + plan + portfolio
python "${CLAUDE_SKILL_DIR}/news.py" TCS SBIN --hours 12           # the same as --tickers TCS SBIN
python "${CLAUDE_SKILL_DIR}/news.py" --tickers INFY --all --hours 72 # three days, seen or not
python "${CLAUDE_SKILL_DIR}/news.py" --no-headlines --json           # the exchange's filings only
python "${CLAUDE_SKILL_DIR}/news.py" --market --hours 6 --tag earnings   # every company's results
```

Exit 0 when something new was shown, 1 when nothing new was, 2 when it refused: nothing to watch,
nothing could be read (every source failed, or no watched symbol is a company the exchange lists:
that is not "no news"), or a flag, setting or state file it cannot use. Other flags: `--tickers
SYM ...`, `--symbol SYM`, `--days N` (the window in days), `--tag TAG`, `--routine`, `--no-filings`,
`--category NAME` (or `--forms`: only the exchange's announcement categories containing NAME, like
`Outcome of Board Meeting`, repeatable or comma separated like the US pack's `--forms 8-K,10-Q`),
`--no-moves`, `--move-pct N`, `--top N` (default 60; a `!` item is printed whatever it says),
`--dry-run` (leave the state alone), `--save` (write `nse/news.json`), `--now` (`2026-10-24 14:30`
or `2026-10-24T14:30`, IST), `--offline DIR`, `--config FILE` (`news_hours`, `news_move_pct`),
`--watchlist`, `--plan`, `--portfolio`, `--state`. `--json` gives `checked`, `hours`, `moves`
(each with `symbol`, `price`, `previous_close`, `pct`, `moving`, `at` the quote's own time, and
the quote's other fields), `items`
(each `id`, `kind` filing or headline, `symbol` where the US pack says `ticker`, `title`,
`category`, `link`, `source`, `time`, `touches`, `tags`, `important`, `new`), `routine_left_out`,
`sources_read`, `read_nothing` and `notes`.

## What it reads and writes

| File / feed | What |
|---|---|
| symbols (or `--tickers`), else `nse/watchlist.json`, `nse/plan.json`, `nse/portfolio.json` | the scanner's hits, the planned trade, what you hold (`symbol` or `ticker` in each row). One that exists but cannot be read, or is not the shape written for it, is said, never skipped quietly |
| `nseindia /api/corporate-announcements` | every company's filings in one request: measured, the market-wide answer holds exactly the rows each company's own answer does |
| Google News, India edition (RSS) | headlines that name the company |
| the keyless chart endpoint | the last price against the previous close (delayed, unofficial) |
| `nse/state/news.json` | what has been shown, kept 30 days, so an item shows once. Only what was printed counts: an item cut by `--top` or a routine filing left out stays new. A damaged state file is refused (exit 2) and left as it is, never read as empty and overwritten; it is written whole or not at all |
| `nse/state/news-market.json` | the same for `--market`, kept apart so a market sweep never uses up the watchlist's news |
| `nse/news.json` | written with `--save`, for your notes; no other skill reads it |

## The rules

- **Filings beat headlines.** The announcement is what the company told the exchange. A headline is
  someone's report of it: a lead, not a fact. Open the filing or the story before saying something
  happened, quote the source and link it, and never summarise a story you could not read.
- **Only headlines that name the company.** Matched on the name it files under, less "Limited",
  its words joined by a space or a hyphen as the US pack matches a name. Matching the trading
  symbol was measured wrong ("ITC" matched GST input-tax-credit stories, "LT" a Lieutenant
  Governor, "TCS" a Malaysian holding company), and a company that files under an abbreviation
  (ITC) must also come with a word tying it to the company ("ITC shares", "ITC Q2"). Stock-quote
  and profile pages ("... Estimates – LSX:903136 - TradingView", "... Earnings Date and
  Estimates") are dropped, judged on the title with its publisher. How many were left out is
  always printed.
- **A one-word name is not proof.** Bosch Limited files as "Bosch" and Siemens Limited as
  "Siemens", names a foreign parent shares, so a headline naming either could be about Robert Bosch
  GmbH or Siemens AG. A one-word name (written as a name: "an eternal wait" is not Eternal Limited)
  counts only with something tying it to the listing here: the full listed name ("Bosch Ltd"),
  the symbol (`NSE: BOSCHLTD`, `(BOSCHLTD)`), "Bosch India" or "India's Bosch", or the Indian
  market, rupees or the Indian quarter in the title (NSE, BSE, Sensex, Nifty, Dalal Street, SEBI,
  Rs, the rupee sign, crore, lakh, Q2FY27; a bare "FY26" is not enough, a company abroad can
  write its own fiscal year that way). This costs real headlines and they are counted by name: measured on the saved
  Infosys search, all 98 headlines were about Infosys and 69 carried no such tie, so 29 are kept.
  A missed headline is a gap; a wrong one is false data. The same rule decides which stocks an
  item touches by name.
- **Why not the US pack's headline feed.** Measured on 2026-10-24 across the 15 largest NSE
  companies over two days: the Yahoo headline feed returned nothing for 9 of the 15, and only 21 of
  its 58 headlines named the company. Google News' India edition answered for 14 of the 15.
- **Say what it touches.** Every item lists the stocks it touches: by the company's name, or by its
  symbol written `NSE: TCS`, `NSE:TCS` or `(TCS)`, never as a bare word. And its tags: earnings,
  analyst, order, legal, deal, capital, people, risk, halt, regulatory, rating, macro. Keyword tags
  are a reading aid, never a fact; the text is printed beside them. `regulatory` needs a regulator
  acting (a SEBI order, penalty, notice, ban), not a filing citing the SEBI rule it is made under.
- **What would read nothing is refused** (exit 2): `--no-filings --no-headlines`, `--market
  --no-filings`, `--category` with `--no-filings`, and `--market` with symbols (it would ignore
  them). So are `--hours` with `--days`, a window of 0 hours or less or one reaching past any
  date, `--top` below 1, a negative `--move-pct`, a `--category` with no name in it, and a
  setting that is not a number. An empty report would look like no news. A watchlist, plan or
  portfolio file that exists but is not the shape written for it is said, like one that cannot be
  read. `--category` keeps filings only, so it asks for no headlines.
- **A symbol the exchange does not list is said**, with headlines on or off: "Nothing new." for a
  typo reads as a quiet day for a company that does not exist. The list is the EQ series; a
  company outside it that filed in the window is said too, and its filings are read. If the list
  itself cannot be read, that is said, and so is what it costs.
- **A window longer than 30 days says so.** Shown items are remembered 30 days, so one shown
  earlier than that would come back as new.
- **"!" marks what the US pack marks in an 8-K**, from the exchange's own categories: results
  (and a board-meeting outcome about results), a director or officer change, insolvency, a payment
  default or fraud, the auditor resigning or changing, a forensic audit, delisting or a trading
  suspension, an open offer, a disruption of operations. Only filings are marked; a headline never.
  A `!` item is never cut by `--top`: measured, a run of five stocks had Infosys' results filing at
  number 105 of 154, behind the headlines about it (49 of 87 once one-word names needed a tie).
- **Routine paperwork is left out unless asked.** Trading-window closures and depository
  certificates alone were 4,282 of 15,294 announcements over thirty days. The count left out is
  printed; `--routine` shows them.
- **MOVING** marks a stock 3% or more from its previous close (`--move-pct`). The line carries the
  quote's own date and time: on a weekend it is Friday's price against Thursday's close; a quote
  with no time says so. A price that is missing, zero or not a number is a note, never a line. A
  move with no news is worth saying out loud too.
- **A cut is marked.** A line holds 160 characters of the title; a longer one ends in `...`.
- **No predictions.** Report what was published and how far the price is from the previous close.

## Steps

1. Run the check: `python "${CLAUDE_SKILL_DIR}/news.py"`. Exit 1 means nothing new; exit 2 means
   it refused or read nothing, so say why (the message or the notes), never "no news".
2. Read the items that matter first: every `!` filing, anything tagged earnings, halt, legal, risk
   or regulatory, and every MOVING stock.
3. Write a short brief per stock: what happened, the source and link, the move, and one line on why
   it matters to the setup (for example "results are due Monday: an overnight gap can jump past any
   stop").
4. If a story changes the picture, say so before `/nse-entry` and `/nse-exit`: results inside the
   trade, a suspension, a fundraise. Never act on a headline alone.

During the session: `/loop 15m /nse-news`, or a scheduled task each morning before the open.

## Example

Saturday 2026-10-24 at 14:30, `news.py --tickers TCS HDFCBANK ITC INFY SBIN` (lines left out are
marked `...`; links shortened):

```
News for TCS, HDFCBANK, ITC, INFY, SBIN, last 48 hour(s).
...
SBIN        Rs 958.60 -1.18% vs the previous close Rs 970.00 (quote of 23 Oct 2026 15:28 IST)

  2026-10-24 11:52 IST  INFY        Mid-Day            Infosys Q2FY27 net profit rises 6.2 per cent to Rs 7,823 crore; cuts revenue guidance
      tags: earnings  https://news.google.com/rss/articles/...
...
  2026-10-23 22:50 IST  HDFCBANK    NSE filing         HDFC Bank Limited has informed the Exchange about Transcript
      Analysts/Institutional Investor Meet/Con. Call Updates  tags: analyst  https://nsearchives.nseindia.com/...
...
! 2026-10-23 16:08 IST  INFY        NSE filing         Infosys Limited has submitted to the Exchange, Financial Results for the quarter and half year ended September 30, 2026 and declaration of interim dividend
      Outcome of Board Meeting  tags: earnings, capital  https://nsearchives.nseindia.com/...
... and 27 more (--top to show them, --json for all).
note: 286 headline(s) left out: 209 did not name the company in the title, 69 named Infosys with nothing tying that one-word name to the company listed here (its full name, its symbol, India, rupees or the Indian market; a foreign parent can share such a name), 8 were stock-quote or profile pages
Checked 2026-10-24 14:30 IST. Headlines are leads, not facts; open the filing or the story before acting. Not financial advice; nothing was placed.
```

When a stock has no news, say so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, or tells the user to buy or sell because of a headline.
- Never hammers a server: one request for the exchange's filings and one for its equity list, one
  per stock for headlines and for the price (a second host, or the exchange's daily file, only when
  the first does not answer), with half a second between the headline searches and between the
  price requests.

## Safety

Education and tooling, not financial advice. This skill only reads and reports. Nothing places an
order.
