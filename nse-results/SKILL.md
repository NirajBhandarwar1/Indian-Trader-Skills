---
name: nse-results
description: >-
  The Results Analyst for NSE stocks. Reads each quarter's results as the company
  filed them with the exchange (XBRL, no key) and sums up what changed: revenue,
  gross, operating and net margins, owners' profit and diluted EPS, quarter by
  quarter, each against the same quarter a year earlier, with the last four
  quarters added up. Gives the next results board meeting from the exchange's own
  notice. Withholds EPS growth across a bonus or split, flags a year whose quarters
  do not add up to the year, and states it when the year-ago quarter was unusual.
  Reads an earnings call transcript when you paste one. Use when the user says
  "how did TCS's results look", "analyze the results", "what changed this
  quarter", "is revenue growing", "check the filing", "compare margins", "sum up
  the call", "does it report this week", or before trading a stock around its
  results. --annual gives four years of accounts.
argument-hint: "[symbols] [--quarters N] [--metrics] [--annual]"
---

# nse-results

Results move stocks more than anything else on the calendar, and the press release is written to
put the best number first. This skill reads the numbers the company actually filed with the
exchange, lines them up quarter by quarter, and says plainly what changed. For a day trader it
answers two questions before a trade: what did the last results say, and is the next one inside
this trade?

**No key, no token, no account.** Standard library Python.

```bash
python "${CLAUDE_SKILL_DIR}/results.py" TCS                       # the last six quarters
python "${CLAUDE_SKILL_DIR}/results.py" TCS SBIN --quarters 8     # two years
python "${CLAUDE_SKILL_DIR}/results.py" --metrics                 # the watchlist, into nse/metrics.json
python "${CLAUDE_SKILL_DIR}/results.py" TCS --annual              # four years of accounts (--ratios, --shares)
```

With no symbols it reads the ones on `nse/watchlist.json`. Other flags: `--basis standalone`,
`--no-filings` (skip the board-meeting notices), `--json`, `--offline DIR`, `--now`,
`--config FILE` (`results_quarters`, `results_basis`), `--watchlist FILE`, `--symbol SYM`.
Exit 0 when every company was read, 1 when some were, 2 when none were, and 2 for a refusal (bad
settings, a metrics file it will not overwrite, `--annual` with `--offline`).

## What it reads and writes

| Source / file | What |
|---|---|
| `nseindia /api/integrated-filing-results` | every quarter from 31-Mar-2025, current to the day: TCS's September 2026 quarter was there within minutes of its 8 October board meeting |
| `nseindia /api/corporates-financial-results` | quarters up to 31-Dec-2024. It stops there because SEBI's integrated filing replaced it; the two join with no gap |
| each filing's XBRL | the figures, read from the quarter's own context |
| `nseindia /api/corporate-board-meetings` | the next results meeting, once the company has given notice |
| `nseindia /api/corporates-corporateActions` | bonuses and splits, so EPS growth is never compared across one |
| `nse/watchlist.json` | the symbols, when none are named (written by /nse-scan) |
| `nse/metrics.json` | written with `--metrics`: one row per company (last four quarters' revenue, owners' profit and EPS, the latest margins and growth, and `notes` / `ttm_notes` for anything withheld or flagged). `metrics.example.json` here is a real run for RELIANCE, TCS, SBIN, HDFCBANK and ITC on 2026-10-24. A file already there that is not a metrics file is refused (exit 2) and left untouched, never overwritten |

- **Earnings calls.** The tool does not fetch transcripts. If the user pastes one (or the
  company's investor-relations page or the exchange's call-presentation link has it), read it and
  sum up what management said changed: guidance, margins, demand, and anything they declined to
  answer. Say it came from the call, not the filing.
- **The next results date** comes from the exchange's board-meeting notice. When none is
  announced, look for it in `/nse-news` headlines or on the company's investor-relations page, and
  say where it came from.

## The rules

- **Filed numbers first.** Every figure in the table is the company's own, from its XBRL filing.
  Checked against a second source: owners' profit and profit before tax match Yahoo's quarterly
  figures to the rupee for TCS, SBI, Reliance, ITC and Suzlon, and Reliance's last-four-quarters
  owners' profit (74,727 crore) equals Yahoo's own trailing figure exactly. Use the press release
  for context (guidance, segment colour), and say which numbers came from where.
- **A filing that cannot be read is refused, never shown blank.** A figure given as NaN, infinity
  or anything else that is not a number, dates it states that disagree with its quarter, a filing that carries no revenue and no profit under the names read (an insurer's
  format, say), a file that says it is the other basis from the one the exchange lists it as, or a
  saved file that is damaged: each refuses that quarter, listed under "Not read".
- **Compare like with like.** Growth is against the same quarter a year earlier. Quarter-on-quarter
  is shown but many businesses are seasonal, and only against the quarter just before: if that
  filing could not be read, no quarter-on-quarter line is given. One basis for every quarter:
  consolidated, or standalone where a company files only that, never the two mixed.
- **The quarter is read by its context, not its dates.** A filing carries the quarter (OneD) and
  the year to date (FourD), and in an annual filing both can carry dates inside the same three
  months. Only OneD is read, and its dates must span a quarter or the filing is refused. Across 146
  filings of 7 companies, every taxonomy version from 2024 to 2026, OneD was a clean quarter every
  time.
- **No quarter is derived.** The US pack builds a fourth quarter as the year minus three 10-Qs.
  Indian companies file the March quarter itself, so every quarter here is filed, none computed.
- **Banks are banks.** They file under their own taxonomy. "Revenue" for a bank is total income as
  filed, operating profit is before provisions as filed, and there is no gross margin.
- **Say what is computed.** An Ind AS company files no operating profit, so it is computed from
  filed lines: profit before exceptional items and tax, plus finance costs, less other income.
  Gross margin is revenue less the materials, purchases and inventory change filed, and blank for a
  company that files none (TCS files zero for all three; a 100% gross margin would be true and
  useless). A line either needs that is not in the filing is never taken as zero: that margin is
  left blank.
- **Each quarter is named by the company's own year.** The filing states its financial year, and
  the quarter is named in it: most run April to March, but a company with an October year calls
  September Q4. A quarter that does not fall on a quarter of that year, or a filing that states no
  year or no currency, is refused and listed under "Not read".
- **EPS is as filed, and its growth is withheld across a bonus or a split.** HDFC Bank filed 21.12
  for June 2025 and 12.47 a year later; its 1:1 bonus went ex on 26 August 2025, so -41% would be a
  false fall. The window runs to the day the newer quarter's results were published, not its last
  day: a bonus in between restates that quarter's EPS (Ind AS 33.64). The last-four-quarters EPS
  is checked the same way. No corporate-action record, one that cannot be read exactly, or a
  bonus or split whose ex-date cannot be read, withholds it too, and the reason is written to
  `nse/metrics.json` as `ttm_notes`. A last-four-quarters total with a quarter that filed no
  figure is left out, and that is said too.
- **Flag one-offs.** When owners' profit is above operating profit (before provisions, for a
  bank), something outside that line is inside it, and the summary says so and lists what can sit
  there, without guessing which.
- **A year whose quarters do not add up is flagged.** The year-end filing carries the full year
  beside its quarter. ITC's FY2025 quarters add to 1,425 crore more revenue than the year as filed,
  the year its hotels were demerged; HDFC Bank's consolidated FY2025 owners' profit differs by
  2,198 crore. Why is not in the filings, so no reason is given: growth leaning on such a year is
  marked "restated base" and the two figures are printed. Enough filings are read for every year
  the shown rows lean on, so the flag does not depend on `--quarters`.
- **A check that could not be made says so.** If a year's four quarters are not all readable, or
  its year-end filing does not carry the whole year, every row leaning on it is marked "base
  unchecked" with "restated check not possible" and the reason. If the older listing (quarters to
  Dec-2024) does not answer, that is printed under "Not read", never skipped in silence.
- **An unusual base is stated, with figures.** Reliance's June 2025 quarter carried 15,119 crore of
  other income against 6,550 crore in June 2026, so its -22.4% profit fall is said beside that.
  Exceptional items in either quarter are said too, and so is profit from discontinued operations:
  ITC's March 2025 quarter carried 14,652.61 crore of it inside 19,727 crore of owners' profit, so
  its March 2026 profit reads -72.7% against that base, and both figures are printed.
- **Say when the filing lags.** If the board has met to approve results after the newest filing
  here, the tool prints a NOTE with the meeting's link: read that announcement directly. If the
  exchange does list a filing from that meeting and it could not be read, the NOTE says that
  instead of "not on the exchange yet". A meeting today is not "held" yet (the notice gives the
  day, not the hour): it is the next meeting.
- **The next report date comes from the exchange.** A results board meeting is listed once the
  company gives notice (ITC: 2 November 2026, notified 16 October). None announced is said as
  that, not as none coming. A meeting on or before the newest filing (read or listed) has already
  reported and is not "next"; a meeting today with no filing yet still is. If the notices cannot be read, that is
  said, the results are still shown, and it is listed under "Not read".
- **Bad settings are refused.** `results_basis` must be "consolidated" or "standalone", and
  `results_quarters` a whole number of at least 1; anything else stops with a plain line.

**The results-day documents are linked.** The US pack links the earnings 8-K's press release beside
the 10-Q. The Indian counterparts are on the exchange's announcements the day the board approved the
results: the results themselves ("Outcome of Board Meeting"), the press release and the earnings-call
presentation. TCS, 8 October 2026: the results at 15:40 and the press release at 15:43. The company's
name is shown, and written to `nse/metrics.json` as `name`.

## Steps

1. Run it for the companies in question, or the watchlist:

   ```bash
   python "${CLAUDE_SKILL_DIR}/results.py" TCS
   ```

2. Read the "Withheld or flagged" lines before the growth figures. Open the results-day press
   release and the filing link when the numbers need context, and read them.
3. Write the summary: one line on the quarter (revenue, profit, EPS, growth), one on margins, one
   on anything flagged (a bonus, a restated base, an unusual base, exceptional items), and the link.
   Plain words, real numbers, no adjectives the numbers do not support.
4. If the next results meeting falls inside the planned trade, say it before `/nse-exit` sets the
   stop: a stop does not protect against a gap, and the stock can open past it.

## Example

`results.py TCS` (2026-10-24): quarter ending 2026-09-30 (Q2 FY27), revenue Rs 73,188 cr, +11.2%
against a year earlier, owners' profit Rs 13,884 cr (+15.0%), diluted EPS Rs 38.37 (+15.0%);
operating margin 24.0%, net margin 19.0%; last four quarters Rs 283,248 cr revenue and Rs 142.64
EPS. Filed 8 October; no results meeting announced yet.

`results.py HDFCBANK`: EPS growth withheld for the four quarters that compare across the August 2025
bonus (Q2 FY26 to Q1 FY27), and four quarters marked "restated base" against FY2025.

## --annual: four years of accounts

`--annual` reads four annual years from a keyless fundamentals feed (a hard cap: asking for 2010,
2000 or 1970 returns the same four), with `--ratios` (ROE, ROA, margins, EBITDA margin,
debt/equity, both halves of each from the same side; a zero profit is 0.0%, and a ratio over
zero or negative equity is left out) and `--shares` (a share count only where the
feed and the exchange's shareholding filing report the identical number, to the share; TCS and
SUNPHARMA at March 2026, not ITC, which sits 3,104,065 shares apart; a source that could not be
reached is said as that, never as "filed none"). `--save` writes `nse/results.json`, every
company under its own symbol. `--json` prints one document for all the companies, warnings
included. It reads a live source only, so `--offline` is refused. Four of that feed's field names
mean something other than they say, and the skill handles each: its "net income" is the owners' share (kept apart from the group's), its cash
field is only part of the cash (the cash-flow closing balance is used), and its share count and
per-share figures are restated to today's basis (the count is proved or withheld; per-share figures
are not read there at all, because the quarterly filings carry the company's own). A year reported
in another currency is refused, never converted: that feed gives Infosys in US dollars, and such a
year is left blank in the table rather than shown under a crore heading.

## What it never does

- Never says a stock is cheap, dear, a buy or a sell. It reports what the company filed.
- Never invents a number the filings do not have, fills a gap, or mixes consolidated with
  standalone.
- Never compares EPS across a bonus or a split, or gives a reason the filings do not carry.
- Never converts a currency.
- Never needs a key and stays well inside the exchange's limits: about two requests a second.

## Safety

Education and tooling, not financial advice. This skill only reads filings. Nothing places an order
except `/nse-broker`, after `/nse-risk` passes and the user types an explicit yes for that order.
