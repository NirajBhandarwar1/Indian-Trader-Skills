---
name: nse-results
description: >-
  Fundamentals. Four years of a company's accounts and the ratios they imply, with
  no key, token or account. Names the four field names that mean something other
  than they say, and reports years whose figures cannot all be right. Use when asked
  what a company earns, what it is worth against its accounts, its return on equity,
  or whether its numbers hold together.
argument-hint: "--symbol SYM [--ratios] [--cross-check] [--json]"
---

# Fundamentals

Four years of accounts per company, and the ratios they imply.

**No key, no token, no account, no signup.** Indian symbols are reachable with a `.NS` suffix on a
keyless endpoint, and it answers for small caps as readily as for Reliance.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python ${CLAUDE_SKILL_DIR}/results.py --symbol RELIANCE               # the accounts
python ${CLAUDE_SKILL_DIR}/results.py --symbol TCS --ratios           # ROE, ROA, margins, debt
python ${CLAUDE_SKILL_DIR}/results.py --symbol RELIANCE --cross-check # against a second source
python ${CLAUDE_SKILL_DIR}/results.py --symbol TCS --save             # write nse/results.json
```

Exit 0 on success, 2 if the source has no accounts for the symbol — which is the right answer for a
fund, a trust, or a very recent listing.

## What it reads and writes

| File | Direction | Who else uses it |
|---|---|---|
| `nse/results.json` | written with `--save` | `nse-scan` for context on a candidate |
| nothing | read | this skill runs on its own |

## The four field names that lie

Each was measured against Reliance's audited accounts rather than read off documentation. These are
the most useful thing in this skill, because every one of them produces a plausible wrong number.

| Field | What it actually is |
|---|---|
| **NetIncome** | profit attributable to **owners**, not the whole group. Reliance 2026: 80,775 crore against a group profit of 95,754. Comparing it with another source's group profit manufactures a 15% disagreement that was never one |
| **CashAndCashEquivalents** | sounds like the cash position and is only part of it. Out by 3.8% to 50.3% against the audited balance sheets 2023–26. **EndCashPosition** is exact all four years, so that is what this reads |
| **BasicEPS** | **restated** for later bonuses and splits. Useful, and not what the company filed at the time — so against an as-filed figure the two sit exactly a bonus factor apart and one looks wrong |
| **OrdinarySharesNumber** | **today's** share count, against every historical year. Reliance's 1:1 bonus went ex 2024-10-28; the field reports 1,353 crore shares for FY2023 and FY2024, which both closed before it, where the real count was 676.6 crore |

That last one is the worst. **Any per-share figure built from it for a year before the latest bonus
is wrong by the bonus factor.** The skill prints these warnings under every table rather than
hoping you read the docs.

## The ratios are computed, and labelled as computed

| Ratio | Built from |
|---|---|
| ROE | owners' profit over owners' equity |
| ROA | group's profit over group's assets |
| margin | owners' profit over revenue |
| EBITDA margin | EBITDA over revenue |
| debt / equity | total debt over owners' equity |

**Both halves of a ratio come from the same side.** Mixing the group's profit with the owners'
equity is the usual way these come out wrong, and it is easy to do because one field is called
"net income".

## It reports years whose figures cannot all be right

Four years from one source means there is no cross-source disagreement available for anything.
Arithmetic is what is left:

- equity above total assets — a company cannot owe its owners more than everything it has
- profit before tax more than half again revenue — possible with large other income, flagged to read
- negative assets or revenue
- **a year reported in a currency other than rupees**, which is refused rather than converted. One
  source reports Infosys in US dollars, and against a rupee share price that is wrong by about
  eighty times with nothing in the numbers to show it

## `--cross-check` is the only thing a token buys

Entirely optional, and nothing needs it. With a token configured, the accounts are compared against
a second source and any figure more than 1% apart is reported. That matters because the two agree to
the rupee on most figures, so a real gap means one of them is wrong and neither should be relied on.

Without a token the skill says nothing is configured and carries on. That is the normal case.

## Data and limits

- **Four years, and that is a hard cap.** `period1` set to 2010, 2000 or 1970 returns the same four,
  on RELIANCE, TCS, HDFCBANK and ITC alike.
- **Consolidated figures.** No standalone option here.
- **Accounts are history**, audited months after the fact.
- The endpoint is unofficial, so it can change or rate-limit without notice.

## Steps

1. `--symbol SYM` and read the contradictions section before the table.
2. `--ratios` for ROE, ROA and margins.
3. **Never build a per-share figure from the share count for a historical year.** Use `/nse-insider`
   and the exchange's corporate actions to find the bonus, or use today only.
4. `/nse-news SYM` for what has been said since the last accounts.
5. `/nse-risk` to size anything.

## Rules

- Print the field warnings under every table, every time.
- Leave a missing figure missing. Never a zero.
- Refuse a non-rupee year rather than converting it.
- Label a computed ratio as computed.

## What it never does

- Never needs a key, a token or an account.
- Never converts a currency.
- Never fills a gap in a series.
- Never builds a per-share figure from a historical share count without the warning.
- Never places or sizes an order.

## Safety

Accounts are history and audited late. A company that earned money last year can lose it this year.
Nothing here is financial advice.
