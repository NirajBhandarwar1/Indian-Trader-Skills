---
name: nse-broker
description: >-
  Order Executor for NSE stocks. Sends approved orders to Zerodha through its
  Kite MCP server, paper by default, never without your yes. Shows each order as
  a plain ticket (entry, stop-loss, target and the max loss at the stop) plus the
  exact arguments for Kite's place_order, records your yes word for word, then
  Claude's own Allow once / Deny card asks again. Protects an intraday trade with
  an SL-M stop once it fills (Zerodha has no bracket order), builds the sell
  ticket that closes a day trade by flat-by, records fills in nse/portfolio.json,
  and logs each trade's max loss so /nse-risk can hold the daily limit. Covers
  setting up Zerodha's Kite MCP server and setting order tools to "Needs
  approval". Use when the user says "place the order", "buy it", "execute",
  "send it to Zerodha", "close my position", "flatten", "square off", "get me
  out", "connect Kite", or after /nse-risk passes.
argument-hint: "show | approve --index N --reply \"yes\" | result | fill | flatten | log"
---

# nse-broker

The Order Executor is the only skill in this pack that can lead to a real order, so it is the
strictest. An order reaches the market only after three separate gates, every time, for every
order:

1. **/nse-risk passed this exact order today** (matched by fingerprint, the same function in both
   skills).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which appears because the order
   tool is set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 41 --filled-price 961.90
python "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol SBIN --side sell --qty 41 --price 950.05
python "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol SBIN --qty 41
python "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply is a real yes for that
order, builds the flatten ticket, and keeps the log in `nse/state/broker.json`. `--now "2026-10-22
10:20"` pins the clock (IST). Other flags: `--index`, `--json`, `--orders`, `--risk`, `--state`,
`--config`, `--today`, `--portfolio`, `--plan`, `--product MIS|CNC|MTF` (flatten), `--date` (fill),
`--note` and `--broker-id` (result), `--all` (log). A refusal exits non-zero with one line saying
why, never a traceback; a damaged file is named and refused, never read as empty or written over.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/orders.json` | the proposed orders (/nse-budget writes it; `flatten` replaces it) |
| `nse/state/risk.json` | /nse-risk's verdicts; only a PASS from today is READY |
| `nse/state/broker.json` | every yes, no, placed, failed and filled, with each entry's max loss; /nse-risk reads it to count what went out today |
| `nse/portfolio.json` | cash and shares, updated by `fill` and by `result --filled-qty`; /nse-budget, /nse-risk, /nse-alerts, /nse-news and /nse-insider read it |
| `nse/plan.json` | the flat-by time for `flatten` |

## How an Indian day trade is protected

Checked against Zerodha's own server source (github.com/zerodha/kite-mcp-server, `mcp/post_tools.go`)
and Zerodha's support pages, October 2026.

- **No bracket order.** Zerodha withdrew bracket orders; `place_order`'s varieties are regular, co,
  amo, iceberg and auction.
- **A GTT one-cancels-other covers delivery, not intraday.** `place_gtt_order` takes a "two-leg"
  trigger (upper and lower), but GTT is published for CNC and MTF equity, not MIS.
- **A cover order cannot be exited through the MCP.** A cover order (variety `co`) carries its stop,
  but exiting its second leg needs a parent order id the MCP's `cancel_order` does not send, so this
  pack does not use it: you could not flatten through Claude.

So an intraday (MIS) trade goes in three steps, each its own tool call and its own Allow once card,
all under the one yes for that order:

1. **The entry**: `place_order`, variety regular, product MIS, BUY, MARKET or LIMIT.
2. **Once it fills, the protective stop**: `place_order`, regular, MIS, SELL, order type SL-M with
   `trigger_price` at the stop, the filled quantity. `result --filled-qty` prints it, and so does
   every later `fill` of the same entry. Until then **no stop exists**. A short entry (a sell that
   is not an exit of shares you hold, any product) is never READY: this pack's portfolio records
   only shares you hold, so the fill and its stop could not be tracked.
3. **The target stays off the broker.** A resting target sell beside the stop would be two sells for
   one position, and if both filled you would be short. /nse-alerts rings at the target; then
   `flatten`.

A **delivery (CNC, or MTF)** order is held overnight, and an SL-M is a DAY order that is gone at
the close, so it is protected by a GTT instead: one `place_gtt_order`, two-leg with a stop and a
target (Zerodha's own one-cancels-other), single with a stop alone. A GTT leg becomes a LIMIT
order when it triggers, so the stop's limit is set 1% below the stop: a gap below that limit can
still leave the sell unfilled, and you can change the limit before placing it. Its `last_price` is
the fill price (on the ticket before any fill, the plan's entry); with no price known it is left
out and the ticket says to add it.

The max loss on a READY ticket is /nse-risk's own figure for that order: the gate measures a market
entry from the higher of the plan's entry and a fresh quote, so the figure is not understated after
the price has run, and it is the figure the log keeps for the daily loss limit. A fill can still be
worse than the stop.

## Setup: Zerodha's Kite MCP server

1. **Orders need your own server.** Zerodha's README: "The hosted version at mcp.kite.trade excludes
   potentially destructive trading operations for security." The hosted server reads holdings,
   positions, orders and margins and is fine for that; to place orders you run the server yourself
   with your own Kite Connect app.
2. **A Kite Connect app.** Kite Connect Personal is free and allows order placement and tracking
   positions, holdings and funds; it has no market data, which this pack does not need. Create it at
   developers.kite.trade.
3. **A static IP.** Under SEBI's rules for retail API trading, Zerodha accepts API orders only from
   an IP address registered on the Kite Connect app. A home connection's address can change;
   register the one you trade from.
4. Build and run the server as its README says, then in Claude Desktop add it to
   `claude_desktop_config.json` (Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "kite": {
      "command": "npx",
      "args": ["mcp-remote", "http://localhost:8080/mcp", "--allow-http"]
    }
  }
}
```

   The API key and secret go in the server's own environment, never in the chat. Never ask for
   them, never read that file back into the conversation, and if the user pastes one anyway, tell
   them to delete the message and regenerate it in the Kite developer console. Log in with the
   server's `login` tool, which gives a Zerodha link; the login happens on Zerodha's page.
5. Restart Claude Desktop.
6. **Set the tool permissions.** Customize > Connectors (Settings > Connectors in older versions),
   open kite, and for each tool pick Always allow, Needs approval or Blocked:
   - `place_order`, `modify_order`, `cancel_order`, `place_gtt_order`, `modify_gtt_order`,
     `delete_gtt_order`: **Needs approval**. Never Always allow.
   - any other tool that places, changes or cancels anything: Needs approval, or **Blocked** if you
     do not use it.
   - read-only tools (`get_profile`, `get_margins`, `get_holdings`, `get_positions`, `get_orders`,
     `get_trades`, `get_gtts`, `get_order_history`, `get_order_trades`): Always allow is fine.
   - To make an instance read-only, the server's `EXCLUDED_TOOLS` setting hides tools. The
     README's example lists only `place_order,modify_order,cancel_order`, which leaves the GTT
     tools able to place orders: list
     `place_order,modify_order,cancel_order,place_gtt_order,modify_gtt_order,delete_gtt_order`,
     plus any mutual fund order tool your version has.
7. Check it: ask Claude for `get_profile` (read only) and confirm it is your own Zerodha account.
8. In Claude Code instead: keep the order tools on ask in `.claude/settings.json`, e.g.
   `"permissions": {"ask": ["mcp__kite__place_order", "mcp__kite__cancel_order",
   "mcp__kite__modify_order", "mcp__kite__place_gtt_order", "mcp__kite__modify_gtt_order",
   "mcp__kite__delete_gtt_order"]}`, and never put an order tool in `allow`.

**Paper.** Zerodha has no paper account. With `"paper": true` (the default) nothing is ever sent:
the ticket shows what would be sent, `approve` says not to call `place_order`, and `result
--status placed --filled-qty Q --filled-price P` records the fill you would have had, marked paper.
Going live is the user's own decision, made by setting `"paper": false` in their own config.
Anything but `true` or `false` there (null, `"false"` as text, 0) is refused, as /nse-risk refuses
it: nothing is shown as READY until it is fixed.

**The portfolio starts from your config.** The first fill with no `nse/portfolio.json` starts it
from `"capital"` (or `"budget"`) in your config and says so. With neither, the fill is refused and
nothing is recorded: there is no default cash figure. Copy `config.example.json` to `config.json`
and set your own capital, or write `nse/portfolio.json` with your real cash.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT READY or BLOCKED goes
   back to `/nse-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order per question. Never offer
   "yes to all".
3. `ticket.py approve --index N --reply "<the user's reply, word for word>"`. If it prints "Not
   recorded", the reply was not an explicit yes for this order (only a reply that starts with "yes"
   or "y" counts, so not "sure", "ok" or "haan"; not "yes to all", a yes naming a
   different stock, the other side or a number not on the ticket, a yes with "but" or "wait" in it).
   Ask again. Never turn a reply into a yes yourself.
4. Only after "Recorded your yes" (and not in paper), call `place_order` with exactly the JSON the
   ticket printed. Claude's card appears: Allow once or Deny.
5. Record what happened: `ticket.py result --index N --status placed --broker-id <order_id>
   --filled-qty Q --filled-price P` (from the response, or `get_orders`/`get_positions`), or
   `--status denied`, or `--status failed --note`. A failed order is not retried without a new check
   and a new yes.
6. Live: place the protective stop it prints, with its own Allow once card, and note its order id
   (on paper nothing is placed; the stop is only printed). Each
   partial fill gets its own stop: record every later fill with `ticket.py fill --side buy`, which
   refuses more than is left of the order and prints the stop for just those shares. A `result
   --status placed` with no fill means no stop exists yet.

## Steps: while it is open, and out by the close

- /nse-alerts watches the stop, the target and the flat-by time (`alerts.py add --from-plan`). The
  SL-M at Zerodha does the stopping.
- When the stop fills, record it: `ticket.py fill --symbol SBIN --side sell --qty 41 --price 950.05`.
  A sale that was not the stop leaves the stop at Zerodha for more shares than you hold; `fill`
  says so and gives the number to change it to (`modify_order`, or `modify_gtt_order` for a GTT)
  or cancel it.
- By flat-by (15:20 IST by default), anything still open is closed:
  1. `get_positions` (read only) for how many shares are held.
  2. `ticket.py flatten --symbol SBIN --qty 41` writes a market sell to `nse/orders.json`. Its
     product comes from today's placed entry in `nse/state/broker.json`; a `--product` that
     disagrees is refused. With no entry today it takes the product of an earlier placed CNC or
     MTF entry; with none, shares whose lots were all bought before today are refused until you
     pass `--product CNC` (or MTF), and otherwise it says it fell back to MIS. An MIS sell of more
     than today's MIS entry filled is refused: the rest would be a new intraday short; so is a
     quantity read from the portfolio when no fill of that entry is recorded yet. A damaged
     `nse/orders.json` or `nse/plan.json` never stops a flatten; it says what it did instead.
  3. `/nse-risk`. A sell that only closes what you hold is never blocked by the daily limits or the
     entry window.
  4. `ticket.py show`, then the user's yes for that one order.
  5. Live: cancel the open SL-M stop first, or the sale and the stop together sell twice: `get_orders`
     shows it (status TRIGGER PENDING), then `cancel_order` with variety regular and its order id
     (Needs approval). A GTT is removed with `delete_gtt_order`. Then `place_order` with the
     ticket, then `result`. On paper nothing was sent, so there is nothing to cancel: just
     `result` after the yes.
- Zerodha squares off MIS positions still open near the end of the session itself, and charges for
  it. Flatten before that.

## Example: the real SBIN trade

```
ORDER 1 of 1, PAPER (nothing is sent: Zerodha has no paper account), Zerodha
  BUY 41 SBIN, market, intraday (MIS), good for today
  stop-loss 950.05
  target 985.45
  max loss about Rs 483.80 if the stop fills at 950.05 (it can fill worse in a fast market, or not at all on a circuit)
  why: orb on SBIN: 41 shares, bound by your max order limit (41 x Rs 11.80 = Rs 483.80 at the stop)
  READY: /nse-risk PASS, waiting for your yes
  note: the stop and target are not at the broker yet: Zerodha has no bracket order, so /nse-broker places the exits itself: MIS entry, then an SL-M at the stop after each fill; the target is never placed at Zerodha for MIS (watched by /nse-alerts); CNC/MTF get a GTT (two-leg, or single-leg when stop only)
  Would call place_order with exactly:
    {"variety": "regular", "exchange": "NSE", "tradingsymbol": "SBIN", "transaction_type": "BUY",
     "quantity": 41, "product": "MIS", "order_type": "MARKET", "validity": "DAY"}
  Once it fills, the protection, place_order (its own yes is this one, paper, so nothing is sent), with the filled quantity:
    {"variety": "regular", "exchange": "NSE", "tradingsymbol": "SBIN", "transaction_type": "SELL",
     "quantity": 41, "product": "MIS", "order_type": "SL-M", "trigger_price": 950.05, "validity": "DAY"}
  The target is not left at Zerodha: a resting target order beside the stop is two orders for one position. /nse-alerts rings at the target; then ticket.py flatten.
  Each fill gets its own stop: a partial fill is protected for what filled, and the rest when it fills.
  Reply "yes" to record this one order on paper (nothing is sent), or "no" to skip it.
```

(`show` prints each JSON one field to a line; the `note:` line is /nse-risk's own note for the
order.) The user replies "yes". Claude records it with `approve`. On paper that is all: nothing
is sent, and `result --status placed --filled-qty 41 --filled-price 961.90` records the fill and
prints the SL-M stop for those 41 shares. Live, Claude calls `place_order` with that JSON; Claude's
card shows the call with two buttons, Allow once and Deny, and the stop gets its own card after the
fill. The `why` and the plan stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates. A yes covers one trade: its entry and its own
  protective stop, nothing else.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, secrets, passwords or the TOTP.
- Never switches `"paper"` to live. The user does that themselves.
- Never leaves a target sell resting beside a stop on an intraday position.
- Never cancels or modifies an order on its own. This trade's own stop (or GTT) is cancelled
  before the flatten sale, under the flatten ticket's yes, or resized when `fill` says it is for
  more shares than you hold, with your yes for that change.
- Never places F&O, MCX or currency orders; /nse-risk blocks derivatives unless the config allows them.
- Never promises a fill price: a market order and an SL-M fill at whatever the market gives, and a
  stock locked at its circuit may not fill at all.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper by default. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except /nse-broker, and only when all three hold: /nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and
  the user clicked Allow once on Claude's own card because the order tool is set to "Needs
  approval". Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
  Zerodha's MIS product offers intraday leverage; this pack sizes every order against cash only.
- Broker keys live in the server's environment, never in the chat.
