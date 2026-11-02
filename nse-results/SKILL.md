---
name: nse-results
description: >-
  Fundamentals. Four years of a company's accounts and the ratios its data source
  publishes against the sector, with five misleading lines deliberately left out and
  named. Checks each year against itself. Use when asked what a company earns, what
  it is worth against its accounts, or whether its numbers hold together.
argument-hint: "--symbol SYM [--basis consolidated|standalone] [--ratios]"
---

# Fundamentals

Four years of accounts per company, and the ratios the source publishes against its sector.

**This is the only skill in the pack that needs a token.** The exchange stopped publishing
structured financial statements in December 2024 — its annual feed stops at 31 March 2024 and its
quarterly feed at 31 December 2024, identically for every company measured, so that is a fact about
the endpoint rather than about any filer. Everything else here works with no account at all.

Get a token free at [upstox.com/developer](https://upstox.com/developer), then put it in
`config.json` as `upstox_access_token` or set `UPSTOX_ACCESS_TOKEN`. Without one this skill says so
and exits cleanly.

Scripts live in `${CLAUDE_SKILL_DIR}`.

## Commands

```bash
python ${CLAUDE_SKILL_DIR}/results.py --symbol RELIANCE              # four years of accounts
python ${CLAUDE_SKILL_DIR}/results.py --symbol RELIANCE --ratios     # published, against the sector
python ${CLAUDE_SKILL_DIR}/results.py --symbol TCS --basis standalone
```

Exit 0 on success, 2 if the source refused, **4 if there is no token** — so a caller can tell a
missing token from a missing company.

## The five lines left out, and why

Each was checked against Reliance's audited accounts. Every one would have produced a plausible
wrong number rather than an obvious one, which is why they are named here instead of quietly mapped.

| Line | What it actually is |
|---|---|
| **Equity Capital** | total equity **including minority interest**, not the owners' share. Reliance 2026 reads 10,85,866 against an audited 9,04,030 — 20% out |
| **Profit After Tax** | struck **before** the share of associates. 95,610 against 95,754 |
| **Total Revenue** | built on revenue net of excise, so not the total income the accounts show |
| **EPS - Basic / Diluted** | **mix two report vintages in one series.** Reliance 2023–26 reads 98.59, 51.45, 51.47, 59.69: the 2024 figure is the comparative restated after that October's bonus, while 2023 is still as filed. The series halves between them where nothing happened |

That last one is the most dangerous, because every individual number is correct. Only the series is
broken, and any growth rate across the join is wrong by the bonus factor.

## What it does show

| Column | Source |
|---|---|
| revenue, other income, profit before tax | income statement |
| total assets | balance sheet |
| cash, cash from operations | cash flow |

All in crore, as reported. Nothing is derived, nothing is interpolated, and a missing figure stays
missing rather than becoming a zero.

## It checks each year against itself

A statement that does not add up is wrong however plausible each line looks, and this is the only
check available on a figure one source reports alone — which is most of them. It reports a year
where profit before tax exceeds revenue plus other income, or where assets or revenue are negative,
and says nothing should be computed from that year.

## The data source is behind Cloudflare, and it refuses Python

`urllib` gets **403 with Cloudflare error 1010**, "access denied based on your browser's signature".
That is a TLS fingerprint check, so no header satisfies it — setting a browser user-agent changes
nothing, because the block is on how Python negotiates the connection rather than on what it claims
to be.

The `curl` binary gets 200 on the same URL with the same token. So this skill shells out to curl,
which ships with Windows 10 and later, macOS and every Linux. The alternative was asking everyone
to install a TLS-impersonation library for one optional skill.

If curl is missing, the skill says exactly that and points out the rest of the pack still works.

## Data and limits

- **Four years only.** The source is capped at four annual periods and no parameter changes it —
  `limit`, `years` and a `from` date were all tried.
- **Tokens expire daily**, around 3:30am India time. A 401 is reported as "replace the token"
  rather than as a missing company.
- **Rate limited at 2,000 requests per 30 minutes**, account-wide. The module keeps a rolling window
  at 900, under half the published limit, and stops for ten minutes on any refusal. One company
  costs three requests.
- **Smaller listings often have nothing here.** That is reported as nothing reported, not as zero.
- Consolidated by default. Standalone is a different set of accounts and they are never mixed.

## Steps

1. `--symbol SYM` for the accounts. Read whether the years add up before anything else.
2. `--ratios` for the source's own P/E, P/B, ROE, ROCE and EV/EBITDA against the sector.
3. Where a published ratio disagrees with one you compute from these figures, **one of the figures
   underneath is wrong** — that disagreement is the most useful signal here.
4. `/nse-news SYM` for what the company has said since.
5. `/nse-insider SYM` for whether anyone inside was buying.
6. `/nse-risk` to size anything.

## Rules

- Never show a refused line, however much it is asked for.
- Report a year that contradicts itself, and say not to compute from it.
- Leave a missing figure missing.
- Say which basis the numbers are on, every time.

## What it never does

- Never derives a ratio and presents it as reported.
- Never mixes consolidated and standalone.
- Never fills a gap in a series.
- Never places or sizes an order.

## Safety

Accounts are history, audited months after the fact, and a company that earned money last year can
lose it this year. Nothing here is financial advice.
