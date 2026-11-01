# NSE Trader Skills

Claude skills for the Indian stock market: read what the exchange published, check who actually
bought, size the position, and refuse the trade when the numbers do not work.

Built because the day-trading skill packs that exist are for US stocks. Nasdaq symbol lists, SEC
EDGAR filings and congressional disclosures do not help you trade NSE — and more to the point, India
publishes something no US source does.

## The delivery percentage

Every exchange publishes volume. NSE also publishes how much of that volume was **taken delivery
of**: shares that moved into someone's demat account instead of being bought and sold again before
the close.

A stock up 8% on huge volume with 12% delivery was traded, not bought. By the close almost everyone
who bought had already sold. The same move on 70% delivery means somebody took the shares home.

No free source outside India publishes this. It is the first thing these skills look at.

## Install

**Claude Code plugin**

```
/plugin marketplace add NirajBhandarwar1/nse-trader-skills
/plugin install nse-trader-skills@nse-trader-skills
```

**Manually**

```bash
git clone https://github.com/NirajBhandarwar1/nse-trader-skills.git
cp -r nse-trader-skills/nse-* ~/.claude/skills/
```

**Requirements:** Python 3.9 or newer. **No pip installs.** The exchange turns away a naive HTTP
client, but a cookie jar and honest browser headers get through, so everything here runs on the
standard library.

## Configuration

Nothing is required. Every NSE-based skill works with no setup.

For position sizing, copy the example and set your capital:

```bash
cp config.example.json config.json
```

`config.json` is gitignored. An Upstox token is optional and only needed for intraday candles and
company fundamentals — the skills that use it say so plainly when it is absent, and the rest carry
on.

## The skills

| Skill | What it does | Needs a token |
|---|---|---|
| **nse-delivery** | real buying against intraday churn; spikes, accumulation, one stock's history | no |
| **nse-news** | corporate announcements, India's 8-K, tagged by what they say | no |
| **nse-insider** | PIT filings, bulk and block deals with the buyer named, institutional flow | no |
| **nse-risk** | position sizing with the full Indian cost stack; answers PASS or BLOCK | no |

More are in progress. Each one is added only once it has been run against live data.

## How they fit together

```
nse-delivery  finds where real money moved
     ↓
nse-insider   names who bought it, and drops the desks turning their book
     ↓
nse-news      says what was announced on that date
     ↓
nse-risk      sizes it, or refuses
```

No skill picks a stock for you, and none of them can place an order.

## Traps these skills already know about

Each of these was found by checking a live answer against what the documentation implied.

- **Exchange traded funds sit near 100% delivery permanently**, because a fund is held rather than
  traded. The daily file carries 2,706 symbols where the equity master list carries 2,359, and
  screening without that filter puts LIQUIDBETF and BANKIETF at the top and not one company.
- **An illiquid stock shows 100% delivery** because its one buyer kept the shares. That is
  arithmetic, not accumulation, so a turnover floor is required before any figure means anything.
- **Shareholding percentages changed convention mid-series.** Up to the June 2025 quarter they are
  percentages (54.48); from September 2025 they are fractions (0.5448), exchange-wide. Reading every
  quarter the same way puts a promoter holding of 5,448% on screen.
- **Insider filings return nothing when asked market-wide.** The endpoint answers 200 with an empty
  list however wide the dates, which reads as "no insider activity anywhere". Ask per symbol and
  INFY returns 197 filings over a year.
- **The useful announcement text is in `attchmntText`.** The exchange's `desc` field is only a
  category and `sm_name` is just the company's name — read that one and every row says "Reliance
  Industries Limited" and tells you nothing.
- **Delivery data is published after the close**, around 7pm India time. Before that, today's file
  does not exist, which is different from a day with no delivery.
- **89% of bulk deals are not positions.** Over 10 days to 2026-10-23, 62 of 70 disclosed bulk
  deals were the same client on both sides of the same stock on the same day. A desk passing stock
  through itself crosses the 0.5% threshold on both legs, so it appears twice, at the top, with the
  biggest numbers. Block deals are the opposite: 16 of 16 were genuinely one-sided.
- **A block deal can be the whole spike.** One institution buying is a different fact from the same
  volume spread over a thousand accounts.

## What these skills never do

- Never name a stock to buy or sell.
- Never place an order. There is no broker connection in this repo.
- Never fill in a day the exchange has not published.
- Never report a partial result as a complete one.

## Not financial advice

This software reports what a public exchange published and does arithmetic on numbers you supply.
It does not predict anything. Nothing here is financial advice, a recommendation, or a solicitation
to trade. Markets lose people money. You are responsible for your own decisions.

## Licence

MIT. See [LICENSE](LICENSE).
