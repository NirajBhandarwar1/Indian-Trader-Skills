#!/usr/bin/env python3
"""News Monitor for NSE stocks. What is new since the last check, and how far each stock has moved.

Reads the exchange's corporate announcements (India's 8-K: results, resignations, orders, defaults,
regulatory actions, timestamped to the minute) and headlines that name each company, for every
stock you watch or hold. Shows only what is new since the last check, newest first, in IST, tags
each item, marks the ones that matter with "!", and never repeats one. Each stock's move against
the previous close sits beside its news, flagged MOVING at 3% or more.

    python news.py                                 # watchlist + plan + portfolio
    python news.py TCS SBIN --hours 12             # the same as --tickers TCS SBIN
    python news.py --tickers TCS --all --hours 72  # everything in three days, seen or not
    python news.py --no-headlines --json           # the exchange's filings only
    python news.py --market --hours 6              # every company's filings, no headlines

Exit 0 when something new was shown, 1 when nothing new was, 2 when it refused: nothing to check,
nothing could be read (which is not the same as no news), or a flag, setting or state file it
cannot use. Headlines are leads, not facts; the exchange filing is what the company said. Nothing
is placed.

Standard library only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# This skill's own copy of the shared code, so the folder works when installed on its own.
# It is written by tools/vendor.py from the one source in nsetools/ and checked byte for
# byte by the tests. Never the parent folder: installed alone, that is a folder of OTHER
# skills, and reaching into it is how a skill works here and breaks everywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nsetools import clock, headlines, nse, quotes, settings, show   # noqa: E402
from nsetools.http import NotPublished                               # noqa: E402

KEEP_DAYS = 30
MOVE_PCT = 3.0
DEFAULTS = {"news_hours": 48, "news_move_pct": MOVE_PCT}

_ACTION = (r"\b(?:orders?|penalt(?:y|ies)|show[- ]cause|notices?|bans?|banned|barred|debar\w*|"
           r"adjudicat\w*|settlement order|interim order)\b")

# What each item is about. Several can apply. Matched on the exchange's category, the company's own
# text and a headline's title alike.
TAGS = [
    ("earnings", r"\bresults?\b|\bearnings\b|\bEPS\b|\bguidance\b|\bQ[1-4]\b|\bquarter(?:ly)?\b|"
                 r"\bnet profit\b|\bPAT\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|\btarget price\b|\bprice target\b|"
                r"\boutperform\b|\bunderperform\b|\bbuy rating\b|\bsell rating\b|"
                r"\binitiates? coverage\b|\bbrokerage\b|analysts?/institutional"),
    ("order", r"\border(?:s)? (?:win|worth|from|of|for)\b|\bbag(?:s|ged|ging)?\b|\bcontract\b|"
              r"\bletter of (?:award|intent)\b|\bawarding of order"),
    ("legal", r"\blitigation\b|\bcourt\b|\btribunal\b|\bNCLT\b|\bNCLAT\b|\barbitration\b|"
              r"\bprobe\b|\binvestigation\b|\bpenalty\b|\bshow cause\b|\borders? passed\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bamalgamation\b|\bdemerger\b|"
             r"\btakeover\b|\bopen offer\b|\bscheme of arrangement\b|\bjoint venture\b|"
             r"\bstake (?:sale|buy)\b|\bdisinvestment\b|\bsale or disposal\b"),
    ("capital", r"\bdividend\b|\bbuy ?back\b|\bbonus\b|\bstock split\b|\brights issue\b|"
                r"\bQIP\b|\bqualified institutional\b|\bpreferential\b|\bfund ?rais|"
                r"\bdebentures?\b|\bNCDs?\b|\ballotment\b|\bIPO\b|\bissue of securities\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bmanaging director\b|\bdirector\b|\bresign|\bappoint|"
               r"\bcessation\b|\bKMP\b|\bchange in management\b|\bretirement\b|\bdemise\b"),
    # "cyber" alone tagged companies named Cybertech and Cyber Media as risks: an incident is meant.
    ("risk", r"\bdefault|\binsolvency\b|\bfraud|\bbankrupt|\bbreach\b|"
             r"\bcyber ?(?:attacks?|incidents?|security|breach(?:es)?)\b|\bstrike|"
             r"\blockout|\bdisruption\b|\bforensic\b|\bclosure of operations\b|\brecall\b"),
    ("halt", r"\bsuspension of trading\b|\btrading (?:halt|suspen)|\bsuspended\b|\bdelist"),
    # Bare "SEBI" was 126 of 137 regulatory tags on a real week, nearly all a filing citing the
    # SEBI regulation it is made under. A regulator acting is meant: SEBI with an action word.
    # "of SEBI" is not enough either: "Notice under Regulation 30 of SEBI" is paperwork.
    ("regulatory", r"\bSEBI(?:'s)?\s+(?:\w+\s+){0,2}?" + _ACTION + r"|" + _ACTION
                   + r"\s+(?:\w+\s+){0,3}?(?:by|from)\s+(?:the\s+)?SEBI\b|"
                   r"\bRBI\b|\blicen[cs]es?\b|\bregulatory approvals?\b|\bUSFDA\b|"
                   r"\bFDA\b|\bCDSCO\b|\bregulator\b|\bministry\b"),
    ("rating", r"\bcredit rating\b|\brating (?:upgrade|downgrade|reaffirm)|\bCRISIL\b|"
               r"\bICRA\b|\bCARE Ratings\b|\bIndia Ratings\b"),
    ("macro", r"\brepo rate\b|\bmonetary policy\b|\binflation\b|\bCPI\b|\bGDP\b|\bUnion Budget\b|"
              r"\bFIIs?\b|\bFPIs?\b|\bmonsoon\b|\bcrude\b|\brupee\b|\bNifty\b|\bSensex\b"),
]

# The exchange's categories that carry what the US pack marks "!" in an 8-K: results (2.02), a
# director or officer change (5.02), insolvency (1.03), a payment default (2.04), the auditor
# leaving (4.01/4.02), delisting or a trading suspension (3.01), a change in control (5.01) and a
# disruption such as a cyber incident (1.05). The category names are the exchange's own, read off
# 15,294 real announcements over thirty days.
IMPORTANT = {
    "Integrated Filing- Financial", "Clarification - Financial Results",
    "Reply to Clarification- Financial results",
    "Reasons for Delayed/Non-submission of Financial Results",
    "Resignation of Director/KMP/SMP", "Resignation", "Change in Management",
    "Change in Director(s)", "Cessation", "Appointment", "Retirement", "Demise",
    "Corporate Insolvency Resolution Process", "Defaults on Payment of Interest/Principal",
    "Fraud/Default/Arrest", "Frauds/Default by employees",
    "Delay/default in the payment of fines/penalties/dues etc. to authority",
    "Resignation of Statutory Auditor", "Change in Auditors", "Initiation of Forensic Audit",
    "Delisting", "Suspension of Trading", "Public Announcement-Open Offer",
    "Disruption of Operations",
}

# Compliance paperwork, left out unless --routine: 4,282 of those 15,294 announcements were trading
# window closures and depository certificates alone. The US pack does the same by watching 8-Ks
# and not every form. How many were left out is always printed.
ROUTINE = {
    "Trading Window", "Certificate under SEBI (Depositories and Participants) Regulations, 2018",
    "Copy of Newspaper Publication", "Structural Digital Database", "Loss of Share Certificates",
    "Issue of Duplicate Share Certificate", "Registrar & Share Transfer Agent Update",
    "Address Change", "Quarterly Compliance Report on Corporate governance - within 21 days from "
    "the end of the quarter", "Trading Plan under PIT",
}


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- what to watch

def _saved(path: Path, problems: list[str] | None):
    """A hand-off file, or None. One that exists but cannot be read is said, never skipped quietly:
    its stocks would silently drop out of the check and the report would look complete."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        if problems is not None:
            problems.append(f"{path} could not be read ({err}), so its stocks are not checked")
        return None


def watch_from_files(portfolio: Path, watchlist: Path, plan: Path,
                     problems: list[str] | None = None) -> list[str]:
    """The scanner's hits, the planned trade and what you hold, in that order.

    A hit or position carries its symbol as "symbol" or, like the US pack's, "ticker"."""
    out: list[str] = []

    def add(symbol):
        symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
        if symbol and symbol not in out:
            out.append(symbol)

    def symbol_of(row):
        if isinstance(row, dict):
            return row.get("symbol") or row.get("ticker")
        return row

    def misshapen(path, wants):
        # Read but not the shape written for it: said, like a file that cannot be read. Iterated
        # as it came, a watchlist holding "TCS" was watched as T, C and S, and a number crashed.
        if problems is not None:
            problems.append(f"{path} is not {wants}, so its stocks are not checked")

    saved = _saved(watchlist, problems)
    hits = saved.get("hits", []) if isinstance(saved, dict) else saved
    if saved is not None and not isinstance(hits, list):
        misshapen(watchlist, 'a list of hits (or {"hits": [...]})')
    for hit in hits if isinstance(hits, list) else []:
        add(symbol_of(hit))
    saved = _saved(plan, problems)
    if saved is not None and not isinstance(saved, dict):
        misshapen(plan, 'one trade ({"symbol": ...})')
    if isinstance(saved, dict):
        add(symbol_of(saved))
    saved = _saved(portfolio, problems)
    rows = saved.get("positions", []) if isinstance(saved, dict) else None
    if saved is not None and not isinstance(rows, list):
        misshapen(portfolio, 'a portfolio ({"positions": [...]})')
    for row in rows if isinstance(rows, list) else []:
        add(symbol_of(row) if isinstance(row, dict) else None)
    return out


def _offline_rows(offline: str, stem: str):
    """The rows of a saved answer (`stem`.json or .json.gz), None if absent; NewsError if damaged."""
    for name in (f"{stem}.json", f"{stem}.json.gz"):
        path = Path(offline) / name
        if path.exists():
            try:
                raw = path.read_bytes()
                body = json.loads(gzip.decompress(raw) if name.endswith(".gz") else raw)
            except (OSError, EOFError, ValueError) as err:
                raise NewsError(f"{path} could not be read ({err})") from None
            rows = body["data"] if isinstance(body, dict) and "_source" in body else body
            if not isinstance(rows, list):
                raise NewsError(f"{path} does not hold a list of rows")
            return [r for r in rows if isinstance(r, dict)]
    return None


def names_for(symbols: list[str], offline: str | None) -> tuple[dict[str, str], str | None]:
    """{symbol: the name it files under}, from the exchange's equity list, and what went wrong if
    it could not be read (the caller says what that costs)."""
    rows = None
    if offline:
        try:
            rows = _offline_rows(offline, "equities")
        except NewsError as err:
            return {}, f"the equity list: {err}"
        if rows is None:
            return {}, f"the equity list: no equities.json in {offline}"
    else:
        try:
            rows = nse.equities()
        except Exception as err:                                 # noqa: BLE001
            return {}, f"the equity list: the exchange's could not be read ({err})"
    wanted = set(symbols)
    return ({str(r["symbol"]).upper(): str(r["name"]) for r in rows
             if str(r.get("symbol") or "").upper() in wanted and r.get("name")}, None)


# ---------------------------------------------------------------- the two feeds

def _ist(text: str | None) -> datetime | None:
    for shape in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime((text or "").strip()[:20], shape).replace(tzinfo=clock.IST)
        except ValueError:
            continue
    return None


def cut(text: str, limit: int) -> str:
    """Shortened to `limit`, with "..." saying so: a sentence cut silently can read as a whole one."""
    return text if len(text) <= limit else text[:max(limit - 3, 0)].rstrip() + "..."


def tidy(raw: str, limit: int = 400) -> str:
    text = re.sub(r"<[^>]+>", " ", str(raw or ""))
    return cut(re.sub(r"\s+", " ", text).strip(), limit)


def announcements(now: datetime, hours: float, offline: str | None) -> list[dict]:
    """The exchange's market-wide announcements over the window, as items. One request."""
    if offline:
        rows = _offline_rows(offline, "announcements")
        if rows is None:
            raise NewsError(f"no announcements.json in {offline}")
    else:
        days = max(1, math.ceil(hours / 24))
        rows = nse.announcements(None, days, now.date())
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        category = tidy(row.get("desc") or "", 120)
        said = tidy(row.get("attchmntText") or "")
        out.append({
            "id": f"nse:{row.get('seq_id') or hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:16]}",
            "kind": "filing", "symbol": str(row.get("symbol") or "").strip().upper(),
            "title": said or (f"{category} (the text is in the attachment)" if category
                              else "(no text given; see the attachment)"),
            "category": category, "link": str(row.get("attchmntFile") or ""),
            "source": "NSE", "time": _ist(str(row.get("an_dt") or row.get("sort_date") or "")),
        })
    return out


def headline_items(symbols: list[str], names: dict[str, str], hours: float,
                   offline: str | None, notes: list[str], heard: list | None = None) -> list[dict]:
    """Headlines naming each company. A symbol with no name is skipped here: main() has already
    said why, once (not in the equity list, or the list itself could not be read)."""
    items, unnamed, pages = [], 0, 0
    untied: dict[str, int] = {}
    days = max(1, math.ceil(hours / 24))
    for symbol in symbols:
        if symbol not in names:
            continue
        try:
            found, a, b, c = headlines.for_company(symbol, names[symbol], days, offline)
        except headlines.NoHeadlines as err:
            # Said even offline, as the US pack says "offline: no file": a stock whose headlines
            # were never read must not look like a stock with no headlines.
            notes.append(f"headlines for {symbol}: {err}")
            continue
        if heard is not None:
            heard.append(symbol)
        items += found
        unnamed += a
        pages += b
        if c:
            untied[headlines.short_name(names[symbol])] = c
    if unnamed or pages or untied:
        said = [f"{unnamed} did not name the company in the title"]
        said += [f"{n} named {name} with nothing tying that one-word name to the company listed "
                 f"here (its full name, its symbol, India, rupees or the Indian market; a foreign "
                 f"parent can share such a name)" for name, n in untied.items()]
        said.append(f"{pages} were stock-quote or profile pages")
        notes.append(f"{unnamed + pages + sum(untied.values())} headline(s) left out: "
                     + ", ".join(said))
    return items


# ---------------------------------------------------------------- tags, touches, the state

# "NSE: TCS", "NSE:TCS" or "(TCS)", never a bare word; one rule with the headline filter's.
names_symbol = headlines.names_symbol


def tag(items: list[dict], watch: list[str], names: dict[str, str]) -> list[dict]:
    """Which of your stocks each item touches, what it is about, and whether it matters.

    A stock is touched by its name on the same rule a headline is kept by: a one-word name like
    "Siemens" needs something tying it to the listing here, or Siemens AG's news lands on it."""
    short = {s: headlines.short_name(n) for s, n in names.items()
             if len(headlines.short_name(n)) >= 4}
    for item in items:
        hay = f"{item.get('category') or ''} {item['title']}"
        touches = {item["symbol"]} if item["symbol"] in watch else set()
        for symbol, name in short.items():
            if headlines.names_company(item["title"], symbol, name):
                touches.add(symbol)
        for symbol in watch:
            if names_symbol(item["title"], symbol):
                touches.add(symbol)
        item["touches"] = sorted(touches)
        item["tags"] = [label for label, rx in TAGS if re.search(rx, hay, re.I)]
        category = item.get("category") or ""
        item["important"] = bool(item["kind"] == "filing" and (
            category in IMPORTANT
            or (category == "Outcome of Board Meeting" and re.search(r"\bresults?\b", hay, re.I))))
        if item["important"] and re.search(r"result", category + " " + hay, re.I) \
                and "earnings" not in item["tags"]:
            item["tags"].insert(0, "earnings")
    return items


def dedupe(items: list[dict]) -> list[dict]:
    """One item per story: the same id, the same link, or the same title on the same day.

    A filing's title key carries the company too. The US pack's filing title always names the
    company ("Tesla filed a 8-K"); the exchange's text often does not, and two companies filing the
    same words on the same day (measured: KICL and KSL, EDELWEISS and RITCO) were merged into one
    item under the first one's link, and in --market the second filing vanished. A headline's does
    not: it names its company, and one story found by two searches is one story, as the US pack
    merges it. The day is India's, whichever zone the feed wrote the time in.
    """
    seen: dict[str, dict] = {}
    out = []
    for item in items:
        keys = [item["id"], item["link"],
                "%s|%s|%s" % ((item.get("symbol") or "") if item["kind"] == "filing" else "",
                              re.sub(r"\W+", " ", item["title"].lower()).strip(),
                              item["time"].astimezone(clock.IST).date() if item["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            hit["touches"] = sorted(set(hit["touches"]) | set(item.get("touches", [])))
            continue
        for key in keys:
            if key:
                seen[key] = item
        out.append(item)
    return out


def fingerprint(item: dict) -> str:
    return hashlib.sha1(item["id"].encode("utf-8")).hexdigest()[:16]


def select(items: list[dict], state: dict, now: datetime, hours: float,
           show_all: bool = False) -> list[dict]:
    """Items inside the window, nothing from after `now`; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff, ahead = now - timedelta(hours=hours), now + timedelta(minutes=10)
    out = []
    for item in items:
        if item["time"] is not None and not cutoff <= item["time"] <= ahead:
            continue
        item["new"] = fingerprint(item) not in seen
        if item["new"] or show_all:
            out.append(item)
    out.sort(key=lambda i: i["time"] or now, reverse=True)
    return out


def load_state(path: Path) -> dict:
    """What has been shown before. None yet is an empty state; a damaged one is refused (NewsError).

    Read as empty, a damaged file showed every item again as new and was then overwritten, losing
    the record; the US pack stops on it too. Fix it, or delete it to start afresh."""
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise NewsError(f"the state file {path} could not be read ({err}); fix it, or delete it "
                        f"to start afresh (everything then shows once more as new)") from None
    seen = state.get("seen", {}) if isinstance(state, dict) else None
    if not isinstance(seen, dict) or not all(isinstance(v, str) for v in seen.values()):
        raise NewsError(f"the state file {path} is not the shape this skill writes (a \"seen\" "
                        f"map of item to time); fix it, or delete it to start afresh")
    return state


def _setting(conf: dict, key: str, default: float, zero_ok: bool = False) -> float:
    """A number from the settings file, refused (ValueError) rather than guessed at.

    `or default` read a 0 as the default and a word like "abc" stopped the run with a traceback."""
    value = conf.get(key)
    if value is None:
        return float(default)
    where = conf.get("_from") or "the settings file"
    try:
        number = float(value) if not isinstance(value, bool) else math.nan
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number) or number < 0 or (number == 0 and not zero_ok):
        raise ValueError(f"{key} in {where} must be a number "
                         f"{'of 0 or more' if zero_ok else 'above 0'}, not {value!r}")
    return number


def remember(items: list[dict], state: dict, now: datetime) -> None:
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for item in items:
        seen.setdefault(fingerprint(item), stamp)
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


# ---------------------------------------------------------------- the moves

def _usable(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def moves(symbols: list[str], offline: str | None, notes: list[str]) -> list[dict]:
    """Each stock's last price against the previous close. Delayed and unofficial.

    Asked one stock at a time with a pause between, as the US pack paces its fetcher."""
    out = []
    asked = 0
    for symbol in symbols:
        try:
            # A saved quote may be packed (.json.gz), as quotes.quote reads it.
            if offline and not any((Path(offline) / f"{symbol}{end}").exists()
                                   for end in (".json", ".json.gz")):
                continue
            if not offline and asked:
                time.sleep(quotes.PAUSE)
            asked += 1
            got = quotes.quote(symbol, offline=offline)
        except (quotes.NoQuote, OSError, ValueError) as err:
            notes.append(f"price for {symbol}: {err}")
            continue
        if not _usable(got.get("price")):
            # A nan or a zero printed as a price is a made-up number, and every comparison against
            # nan is False, so it would never even be flagged.
            notes.append(f"price for {symbol}: the feed gave no usable price ({got.get('price')!r})")
            continue
        if not _usable(got.get("previous_close")):
            notes.append(f"price for {symbol}: no previous close reported")
            continue
        got["pct"] = round((got["price"] / got["previous_close"] - 1) * 100, 2)
        out.append(got)
    return out


# ---------------------------------------------------------------- the report

def _when(item: dict) -> str:
    return (item["time"].astimezone(clock.IST).strftime("%Y-%m-%d %H:%M IST")
            if item["time"] else f"{'time unknown':<20}")


def within_top(items: list[dict], top: int) -> list[dict]:
    """The newest `top` items, and every "!" item however far down, still newest first.

    Measured: on the saved Saturday the five watched stocks had 154 items in 48 hours, and Infosys'
    own results filing, the one "!" among them, was number 105, behind headlines about it (49 of
    87 once one-word names needed a tie). "Read every ! filing first" cannot be done with the "!"
    filing cut."""
    return [item for n, item in enumerate(items) if n < top or item.get("important")]


def render(items: list[dict], notes: list[str], now: datetime, found_moves: list[dict],
           move_pct: float, routine_left: int, top: int, blind: bool = False) -> list[str]:
    out = []
    for m in found_moves:
        # The quote's own date and time, never assumed to be today: on a weekend the last price
        # is Friday's and the close it is measured against is Thursday's.
        # With no time at all the price could be any day's, so that is said rather than left blank.
        at = " (the feed gave no time for this quote)"
        if m.get("at"):
            try:
                at = " (quote of " + datetime.fromisoformat(m["at"]).astimezone(
                    clock.IST).strftime("%d %b %Y %H:%M IST") + ")"
            except (TypeError, ValueError):
                pass
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        out.append(f"{m['symbol']:<11} Rs {m['price']:,.2f} {m['pct']:+.2f}% vs the previous close "
                   f"Rs {m['previous_close']:,.2f}{at}  {flag}".rstrip())
    if out:
        out.append("")
    if not items and blind:
        # Every source failed or knew nothing of the names: that is not the same as no news.
        out.append("Nothing could be read, so this is NOT a sign of no news: see the notes.")
    elif not items:
        out.append("Nothing new.")
    kept = within_top(items, top)
    for item in kept:
        mark = "!" if item.get("important") else " "
        who = ",".join(item["touches"]) or item["symbol"]
        source = item["source"] if item["kind"] == "headline" else "NSE filing"
        out.append(f"{mark} {_when(item)}  {who:<11} {source[:18]:<18} {cut(item['title'], 160)}")
        extra = []
        if item.get("category"):
            extra.append(item["category"])
        if item["tags"]:
            extra.append("tags: " + ", ".join(item["tags"]))
        if item["link"]:
            extra.append(item["link"])
        if extra:
            out.append("      " + "  ".join(extra))
    if len(items) > len(kept):
        out.append(f"... and {len(items) - len(kept)} more (--top to show them, --json for all).")
    if routine_left:
        notes.append(f"{routine_left} routine compliance filing(s) left out (trading window, "
                     f"depository certificates and the like); --routine shows them")
    for note in notes:
        out.append(f"note: {note}")
    out.append(f"Checked {now.astimezone(clock.IST):%Y-%m-%d %H:%M} IST. Headlines are leads, not "
               f"facts; open the filing or the story before acting. Not financial advice; "
               f"nothing was placed.")
    return out


def _ready(items: list[dict]) -> list[dict]:
    return [dict(i, time=i["time"].isoformat() if i["time"] else None) for i in items]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", metavar="SYM",
                    help="the stocks to check, like /nse-news TCS (the same as --tickers)")
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--symbol", help="one company (the same as --tickers SYM)")
    ap.add_argument("--market", action="store_true",
                    help="every company's filings (no headlines, no moves)")
    ap.add_argument("--watchlist", default="nse/watchlist.json")
    ap.add_argument("--plan", default="nse/plan.json")
    ap.add_argument("--portfolio", default="nse/portfolio.json")
    ap.add_argument("--state", default=None,
                    help="what has been shown (default nse/state/news.json; with --market "
                         "nse/state/news-market.json, so a market sweep never uses up your "
                         "watchlist's news)")
    ap.add_argument("--hours", type=show.number, default=None,
                    help="ignore items older than this (default 48)")
    ap.add_argument("--days", type=show.number, default=None, help="the same window in days")
    ap.add_argument("--all", action="store_true", help="show items already seen too")
    ap.add_argument("--tag", choices=[t for t, _ in TAGS], help="only items with this tag")
    ap.add_argument("--category", "--forms", dest="category", action="append",
                    help="only the exchange's announcement categories containing this, like "
                         "'Outcome of Board Meeting' or 'Credit Rating' (repeatable, or "
                         "comma separated like the US pack's --forms 8-K,10-Q)")
    ap.add_argument("--routine", action="store_true",
                    help="include routine compliance filings (trading window and the like)")
    ap.add_argument("--no-headlines", action="store_true", help="the exchange's filings only")
    ap.add_argument("--no-filings", action="store_true", help="headlines only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per stock")
    ap.add_argument("--move-pct", type=show.number, default=None,
                    help="flag a move this big, in percent (default 3)")
    ap.add_argument("--top", type=int, default=60, help="how many items to print (default 60)")
    ap.add_argument("--config", help="your settings file")
    ap.add_argument("--offline", metavar="DIR", help="saved answers, no network")
    ap.add_argument("--now", help="pin the clock, like '2026-10-24 09:00' (IST)")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--save", action="store_true", help="also write nse/news.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    def refuse(why: str) -> int:
        print(f"news: {why}", file=sys.stderr)
        return 2

    try:
        conf = settings.load(args.config, DEFAULTS)
        # The US pack's --now is ISO, so "2026-10-24T14:30" is read the same as "2026-10-24 14:30".
        now = clock.now(args.now.strip().replace("T", " ", 1) if args.now else None)
    except (FileNotFoundError, ValueError) as err:
        return refuse(str(err))
    if args.no_filings and args.no_headlines:
        return refuse("--no-filings and --no-headlines together leave nothing to read. Drop one.")
    named = [*(args.symbols or []), *(args.tickers or []), *([args.symbol] if args.symbol else [])]
    if args.market and named:
        return refuse("--market reads every company's filings, so the symbols given would be "
                      "ignored. Drop --market, or drop the symbols.")
    if args.market and args.no_filings:
        return refuse("--market reads only the exchange's filings, so --no-filings leaves nothing "
                      "to read. Drop one.")
    if args.category and args.no_filings:
        return refuse("--category keeps only the exchange's filings, so with --no-filings nothing "
                      "is left. Drop one.")
    if args.hours is not None and args.days is not None:
        return refuse("--hours and --days are the same window; give one.")
    try:
        hours = (args.hours if args.hours is not None else
                 args.days * 24 if args.days is not None else
                 _setting(conf, "news_hours", 48))
        move_pct = (args.move_pct if args.move_pct is not None else
                    _setting(conf, "news_move_pct", MOVE_PCT, zero_ok=True))
    except ValueError as err:
        return refuse(str(err))
    if hours <= 0:
        return refuse(f"the window must be more than 0 hours, not {hours:g}")
    try:
        now - timedelta(hours=hours)
    except OverflowError:
        # Measured: --hours 1e9 and --days 1e12 stopped the run with a traceback.
        return refuse(f"a window of {hours:g} hours reaches back past any date there is")
    if move_pct < 0:
        return refuse(f"--move-pct must be 0 or more, not {move_pct:g}")
    if args.top < 1:
        return refuse(f"--top must be 1 or more, not {args.top}")
    # Split on commas like the US pack's --forms, but not a comma before a year: the exchange's
    # own "Certificate under SEBI (...) Regulations, 2018" is one category.
    wanted = [part.strip().lower() for c in args.category or []
              for part in re.split(r",(?!\s*\d)", c) if part.strip()]
    if args.category and not wanted:
        # An empty filter kept nothing and said "Nothing new.", which reads as a quiet day.
        return refuse("--category was given no category name. Name one, like 'Credit Rating'.")

    problems: list[str] = []
    if args.market:
        watch: list[str] = []
    else:
        watch = []
        for symbol in named:
            symbol = symbol.strip().upper()
            if symbol and symbol not in watch:
                watch.append(symbol)
        if not watch:
            watch = watch_from_files(Path(args.portfolio), Path(args.watchlist), Path(args.plan),
                                     problems)
        if not watch:
            return refuse("nothing to watch. " + "".join(p + ". " for p in problems)
                          + "Pass symbols (or --tickers), run /nse-scan first (it writes "
                          "nse/watchlist.json), or use --market for every company.")

    state_path = Path(args.state or ("nse/state/news-market.json" if args.market
                                     else "nse/state/news.json"))
    try:
        state = load_state(state_path)
    except NewsError as err:
        return refuse(str(err))

    notes: list[str] = list(problems)
    if hours > KEEP_DAYS * 24 and not args.all:
        notes.append(f"the window is longer than the {KEEP_DAYS} days a shown item is remembered, "
                     f"so one shown more than {KEEP_DAYS} days ago is shown again as new")
    items: list[dict] = []
    heard: list[str] = []
    filed: list[dict] = []
    if not args.no_filings:
        try:
            filed = announcements(now, hours, args.offline)
            heard.append("exchange")
        except (NotPublished, NewsError) as err:
            notes.append(f"the exchange's announcements: {err}")
        if watch:
            filed = [i for i in filed if i["symbol"] in watch]
        items += filed
    names: dict[str, str] = {}
    unknown: list[str] = []
    if watch:
        names, why = names_for(watch, args.offline)
        # --category keeps filings only, so a headline search would be asked for and thrown away.
        searching = not args.no_headlines and not wanted
        if why:
            notes.append(why + (", so no company names to search headlines for" if searching else
                                ", so whether each symbol is a listed company is not checked"))
        else:
            # Said whichever feeds are on: with --no-headlines a mistyped symbol otherwise came
            # back as "Nothing new.", which reads as a quiet day for a company that does not exist.
            # A company outside the EQ series is not in the list but does file: that is said too.
            filers = {i["symbol"] for i in filed}
            for symbol in watch:
                if symbol in names:
                    continue
                if symbol in filers:
                    notes.append(f"{symbol} is not in the exchange's EQ-series equity list, so no "
                                 f"headlines are searched for it; its filings are read")
                else:
                    unknown.append(symbol)
                    notes.append(f"{symbol} is not in the exchange's EQ-series equity list and "
                                 f"filed nothing in the window: check the symbol. Nothing can be "
                                 f"said about it")
        if searching:
            # The search counts days back from the real today. With --now pinned in the past it
            # reaches back to the window's start, or it misses the window and says nothing.
            back = hours if args.offline else hours + max(
                0.0, (clock.now() - now).total_seconds() / 3600)
            items += headline_items(watch, names, back, args.offline, notes, heard)

    items = dedupe(tag(items, watch, names))
    if wanted:
        items = [i for i in items if i["kind"] == "filing"
                 and any(w in (i.get("category") or "").lower() for w in wanted)]
    if args.tag:
        items = [i for i in items if args.tag in i["tags"]]

    shown = select(items, state, now, hours, args.all)
    # Counted among what would have been shown, so the number printed is the number hidden.
    routine_left = 0
    if not args.routine:
        routine_left = sum(1 for i in shown if i.get("category") in ROUTINE)
        shown = [i for i in shown if i.get("category") not in ROUTINE]
    found_moves = [] if (args.no_moves or not watch) else moves(watch, args.offline, notes)
    # What reaches the reader, and so the only thing marked seen. The US pack prints every item it
    # remembers; here --top cuts the printed list and routine filings are left out, and marking
    # those seen meant "--top to show them" and "--routine shows them" then showed nothing.
    printed = shown if args.json else within_top(shown, args.top)
    # Read nothing: every source failed, or not one watched symbol is a company the exchange knows.
    blind = not heard or bool(watch) and len(unknown) == len(watch)

    if args.json:
        if routine_left:
            notes.append(f"{routine_left} routine compliance filing(s) left out (trading window, "
                         f"depository certificates and the like); --routine shows them")
        print(json.dumps({"checked": now.isoformat(), "hours": hours,
                          "moves": [dict(m, moving=abs(m["pct"]) >= move_pct)
                                    for m in found_moves],
                          "items": _ready(shown), "routine_left_out": routine_left,
                          "sources_read": len(heard), "read_nothing": blind,
                          "notes": notes}, indent=2, default=str))
    else:
        if watch:
            print(f"News for {', '.join(watch)}, last {hours:g} hour(s).\n")
        else:
            print(f"Every company's filings, last {hours:g} hour(s).\n")
        print("\n".join(render(shown, notes, now, found_moves, move_pct, routine_left,
                               args.top, blind=blind)))
    if args.save:
        try:
            where = show.hand_off("news.json", _ready(shown))
        except OSError as err:
            return refuse(f"nse/news.json could not be written ({err})")
        if not args.json:
            print(f"wrote {where}")
    if not args.dry_run:
        remember(printed, state, now)
        # Whole or not at all: into a file beside it, then moved over it. Cut short by a crash, a
        # state file written in place is damaged, and the next check refuses it.
        spare = state_path.with_name(state_path.name + ".tmp")
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            spare.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            os.replace(spare, state_path)
        except OSError as err:
            try:
                spare.unlink()
            except OSError:
                pass
            # Said and refused: the next check would show all of this again as new.
            print(f"news: the state file {state_path} could not be written ({err}), so what was "
                  f"shown is not remembered", file=sys.stderr)
            return 2
    if blind and not shown:
        # Every source failed or knew nothing of the names. Exit 1 says "nothing new" to a loop,
        # which this is not.
        return 2
    return 0 if any(i["new"] for i in printed) else 1


if __name__ == "__main__":
    show.run(main)
