---
name: nse-news
description: >-
  Announcements. Reads NSE corporate announcements, India's equivalent of an 8-K, for
  one company or the whole market. Tags results, orders, fundraises, payouts, board
  changes, deals, trouble and rating actions. Use when asked what a company has
  announced, why a stock moved, or what was filed today.
argument-hint: "[--symbol SYM] [--days N] [--tag result|order|fundraise|payout|people|deal|trouble|ratings]"
---

# Announcements

Every material event at a listed Indian company is filed with the exchange, timestamped to the
minute, and public immediately. A result, an order win, a resignation, a fundraise, a rating action
or a regulatory notice all arrive here first.

This is the fastest public signal on the Indian market and it needs no account. The skill reports
what was filed; it does not judge whether the filing matters.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python ${CLAUDE_SKILL_DIR}/news.py --days 1                      # everything filed today
python ${CLAUDE_SKILL_DIR}/news.py --symbol RELIANCE --days 30   # one company, a month
python ${CLAUDE_SKILL_DIR}/news.py --days 2 --tag result         # only results
python ${CLAUDE_SKILL_DIR}/news.py --days 1 --tag trouble        # defaults, penalties, NCLT
python ${CLAUDE_SKILL_DIR}/news.py --days 1 --save               # also write nse/news.json
```

`--top N` limits rows (default 40), `--json` prints machine-readable output. Exit code 0 on
success, 2 if the exchange gave nothing.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/news.json` | written with `--save` | `nse-scan` to explain a mover, `nse-results` for context |
| nothing | read | this skill runs on its own |

## The tags

A filing is tagged by what its text says, not by the exchange's category, which is often just
"Updates".

| Tag | Catches |
|---|---|
| `result` | financial results, earnings calls, press releases about them |
| `order` | orders, contracts, letters of intent |
| `fundraise` | QIP, preferential issue, rights issue, debentures |
| `payout` | dividend, bonus, split, buyback |
| `people` | appointments, resignations, cessations |
| `deal` | acquisitions, mergers, divestments, stake sales |
| `trouble` | default, insolvency, NCLT, penalty, show-cause, auditor resignation |
| `ratings` | credit rating actions |

The list is deliberately short. A long keyword list becomes a filter that hides things, and the job
here is to show what was filed. An untagged filing is not an unimportant one.

## Data and limits

- **The useful text is in `attchmntText`**, which is what the company wrote. The exchange's `desc`
  field is only a category, and `sm_name` is just the company's name — reading that one makes every
  row say "Reliance Industries Limited" and tell you nothing. This skill reads the right field
  because the wrong one was tried first.
- **Volume is high.** About 1,470 announcements are filed on an ordinary day across the market. Use
  `--symbol` or `--tag` unless you genuinely want all of them.
- **Many filings carry only an attachment.** The script says so rather than printing an empty line.
  The PDF URL is in the JSON output.
- **Timestamps are India time** and can fall well after the close; filings at 10 or 11pm are normal.

## Steps

1. `--days 1` to see what was filed today, or `--symbol SYM` for one company.
2. Narrow with `--tag` when looking for a particular kind of event.
3. For anything that looks material, check `/nse-delivery SYM` — an announcement nobody acted on
   shows up as ordinary delivery, and one that moved real money does not.
4. Check `/nse-insider SYM` for insider or bulk-deal activity around the same date.
5. `/nse-results SYM` before drawing any conclusion about value.

## Example

```
$ python news.py --symbol RELIANCE --days 30 --top 3

when              symbol      tag       kind             what was filed
----------------  ----------  --------  ---------------  ----------------------------------------
2026-10-21 20:43  RELIANCE    ratings   Credit Rating    Reliance Industries Limited has infor...
2026-10-16 20:58  RELIANCE              Copy of Newsp..  Newspaper Clippings Proposed Initial ...
2026-10-14 20:12  RELIANCE              General Updates  Proposed Initial Public Offer of Jio ...
```

## Rules

- Report what was filed, in the company's own words where there are any.
- Say how many filings matched and how many are being shown.
- Never summarise a filing you only have the title of; say the attachment carries it.
- Keep the timestamp. When a filing arrived is often the point.

## What it never does

- Never names a stock to buy or sell.
- Never infers a price move from a filing.
- Never places, prepares or sizes an order.
- Never treats an untagged filing as unimportant.

## Safety

An announcement is a fact about a filing, not about a price. Nothing here is financial advice, and
no part of this skill can place a trade.
