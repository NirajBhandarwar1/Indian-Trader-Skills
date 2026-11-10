"""Headlines that name an Indian company, from a free keyless news feed.

WHY THIS FEED AND NOT THE ONE THE US PACK USES. Measured on 2026-10-24 across the 15 largest NSE
companies over two days: the Yahoo headline feed the US pack reads returned nothing at all for 9 of
the 15 (Reliance, HDFC Bank, ICICI Bank, Bharti Airtel, ITC, HUL, Bajaj Finance, Kotak, Sun
Pharma), and only 21 of its 58 headlines named the company -- SBI's top story was about Japan's
SBI Holdings. Google News' India edition answered for 14 of the 15, from Indian newsrooms
(Moneycontrol, Economic Times, Business Standard, Mint, NDTV Profit) as well as Reuters.

THREE RULES keep it from saying things it should not:

  * A headline is kept only if its title NAMES THE COMPANY, by the name it files under with the
    exchange less "Limited". Matching the trading symbol instead was tried and measured wrong:
    "ITC" matched input tax credit claims, "LT" a Lieutenant Governor and "TCS" a Malaysian holding
    company. A name-match misses some short forms ("SBI" for State Bank of India), which is the
    safe direction: a missed headline is a gap, a wrong one is a false statement about a company.
  * A ONE-WORD name ("Bosch", "Siemens", "Infosys") must also come with something tying it to the
    company listed here, since a foreign parent can share it (see INDIA_TIES). How many were left
    out for that is reported on its own.
  * Quote and profile pages are dropped. They come back as search results and are not news:
    "<company> Limited (X.NS) stock price, news, quote and history", "<company> Limited Estimates
    - TradingView", "... Stock Forecasts", "... Share Price Prediction". How many were left out is
    reported, never hidden.

Headlines are leads, not facts. The exchange filing is what the company said.

Standard library only.
"""
from __future__ import annotations

import email.utils
import gzip
import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

SEARCH = "https://news.google.com/rss/search"
BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/122.0.0.0 Safari/537.36")

# One search per company, asked no faster than this apart, as the US pack paces its fetcher.
PAUSE = 0.5
_last_asked = 0.0

# Search-result pages that are not news. Each pattern is a page type seen in the measured run.
NOT_NEWS = re.compile(
    r"stock price, news, quote|\bestimates\b.*tradingview|stock forecasts?\b|"
    r"share price prediction|share price - live|stock price & chart|\bquote and history\b|"
    r"\bestimates\s*[–-]\s*[A-Z]+:\S+|\bearnings date and estimates\b",
    re.I)


ABBREVIATION = re.compile(r"[A-Z0-9&]{2,5}")
TIES = (r"Ltd|Limited|shares?|stock|scrip|Q[1-4]|results?|profit|revenue|dividend|board|"
        r"CEO|MD|chairman|order|stake|share price")

# A ONE-WORD NAME IS NOT PROOF. Bosch Limited files as "Bosch" and Siemens Limited as "Siemens",
# and a headline naming either is as likely to be about Robert Bosch GmbH or Siemens AG abroad:
# reading it as the Indian listing is a false statement about the stock. So a one-word name must
# come with something that ties it to the company listed here: its full listed name ("Bosch Ltd"),
# its symbol in a listed shape ("NSE: BOSCHLTD", "(BOSCHLTD)"), "Bosch India" or "India's Bosch",
# or the Indian market, rupees or the Indian quarter written in the title (NSE, BSE, Sensex, Nifty,
# Dalal Street, SEBI, Rs, a rupee sign, crore, lakh, Q2FY27). A bare "FY26" is not enough: a
# company abroad can write its own fiscal year that way. Measured on the saved Infosys search, every one of its 98
# headlines was about Infosys and 69 carry no such tie, so 29 are kept and the 69 are counted: a
# missed headline is a gap, a wrong one is false data.
INDIA_TIES = re.compile(
    r"\b(?:NSE|BSE|Sensex|Nifty|Dalal Street|SEBI)\b|\bRs\.?\s?\d|₹|\bINR\b|"
    r"\bcrores?\b|\blakhs?\b|\bQ[1-4]\s?FY\s?'?\d{2}\b", re.I)


class NoHeadlines(Exception):
    """The feed could not be read. Said, never treated as 'no news'."""


def short_name(legal: str) -> str:
    """'Tata Consultancy Services Limited' -> 'Tata Consultancy Services'."""
    return re.sub(r"\s+(limited|ltd\.?)\s*$", "", (legal or "").strip(), flags=re.I).strip()


def _words(name: str) -> str:
    """The name as a pattern, its words joined by a space or a hyphen, as the US pack matches it."""
    return r"[\s-]+".join(re.escape(w) for w in name.split())


def mentions(title: str, name: str) -> bool:
    """Whether the title writes the name at all, as a whole word, in any case."""
    return bool(name) and re.search(r"(?<![A-Za-z0-9])" + _words(name) + r"(?![A-Za-z0-9])",
                                    title, re.I) is not None


def names_symbol(title: str, symbol: str) -> bool:
    """Whether a title writes the trading symbol in a shape that means the listed stock.

    The US pack matches "(TSLA)", "$TSLA" and "NASDAQ: TSLA". Here: "NSE: TCS", "NSE:TCS" and
    "(TCS)", in the symbol's own case, never as a bare word ("ITC" alone is also input tax
    credit). And "(ITC)" right after words whose initials spell it is an abbreviation being
    explained, not the stock: measured, "Input Tax Credit (ITC) on Renewable Energy".
    """
    if not symbol:
        return False
    if re.search(r"\bNSE\s*:\s*" + re.escape(symbol) + r"(?![A-Za-z0-9])", title):
        return True
    for found in re.finditer(r"\(" + re.escape(symbol) + r"\)", title):
        words = re.findall(r"[A-Za-z0-9&]+", title[:found.start()])[-len(symbol):]
        if "".join(w[0] for w in words).upper() != symbol.upper():
            return True
    return False


def one_word(name: str) -> bool:
    return bool(name) and len(name.split()) == 1


def names_company(title: str, symbol: str, name: str) -> bool:
    """Whether the title names THIS listed company: the name, and for a short or one-word name
    something that ties it to the listing here (see TIES and INDIA_TIES above)."""
    if not mentions(title, name):
        return False
    word = re.escape(name)
    if ABBREVIATION.fullmatch(name):
        # A company that files under an abbreviation (ITC Limited is "ITC") collides with ordinary
        # words: measured, "ITC" headlines included GST input-tax-credit stories. So an
        # abbreviation must come with a word that ties it to the listed company, in its own case.
        return bool(re.search(r"(?<![A-Za-z0-9])" + word + r"(?![A-Za-z0-9])\s*(?:['’]s\s+)?(?i:"
                              + TIES + r")\b|\bNSE\s*:\s*" + word + r"\b", title)
                    or names_symbol(title, symbol))
    if one_word(name):
        # Written as a name, not a word: "Eternal" or "ETERNAL" for Eternal Limited, never "an
        # eternal love" (its first letter capital, or as the company writes it, like "eMudhra").
        if not any(m.group()[0].isupper() or m.group()[0] == name[0] for m in re.finditer(
                r"(?<![A-Za-z0-9])" + word + r"(?![A-Za-z0-9])", title, re.I)):
            return False
        return bool(re.search(r"(?<![A-Za-z0-9])" + word + r"\s+(?:Ltd\b|Limited\b|India\b)|"
                              r"\bIndia['’]s\s+" + word + r"(?![A-Za-z0-9])", title, re.I)
                    or names_symbol(title, symbol) or INDIA_TIES.search(title))
    return True


def url_for(name: str, days: int) -> str:
    return SEARCH + "?" + urllib.parse.urlencode(
        {"q": f'"{name}" when:{max(1, days)}d', "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})


def _fetch(url: str, timeout: float = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER,
                                                   "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body
    except (urllib.error.URLError, OSError) as err:
        raise NoHeadlines(f"the news feed could not be reached: {err}") from err


def parse(raw: bytes, symbol: str, name: str) -> tuple[list[dict], int, int, int]:
    """(kept items, how many did not name the company, how many were quote or profile pages,
    how many wrote a one-word name with nothing tying it to the listed company)."""
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as err:
        raise NoHeadlines(f"the news feed answered something unreadable: {err}") from err
    kept, unnamed, pages, untied = [], 0, 0, 0
    for item in root.iter("item"):
        title = raw_title = (item.findtext("title") or "").strip()
        publisher = (item.findtext("source") or "").strip()
        # The feed appends " - Publisher" to every title; the publisher is carried separately.
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].rstrip()
        if not names_company(title, symbol, name):
            if one_word(name) and not ABBREVIATION.fullmatch(name) and mentions(title, name):
                untied += 1
            else:
                unnamed += 1
            continue
        # Read on the title as served, publisher and all: a profile page is often told apart only
        # by it ("... Estimates - TradingView"), and cutting it first hid every such page.
        if NOT_NEWS.search(raw_title):
            pages += 1
            continue
        when = None
        try:
            when = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError):
            when = None
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        kept.append({"id": "news:" + hashlib.sha1(guid.encode("utf-8")).hexdigest()[:20],
                     "kind": "headline", "symbol": symbol, "title": title, "link": link,
                     "source": publisher or "news", "time": when, "category": None})
    return kept, unnamed, pages, untied


def for_company(symbol: str, legal_name: str, days: int,
                offline: str | None = None) -> tuple[list[dict], int, int, int]:
    """Headlines naming one company over the last `days` days."""
    global _last_asked
    name = short_name(legal_name)
    if not name:
        raise NoHeadlines(f"{symbol}: no company name to search for")
    if offline:
        path = Path(offline) / f"headlines-{symbol.upper()}.xml"
        if not path.exists():
            raise NoHeadlines(f"{symbol}: no saved headlines in {offline}")
        raw = path.read_bytes()
    else:
        wait = PAUSE - (time.monotonic() - _last_asked)
        if _last_asked and wait > 0:
            time.sleep(wait)
        _last_asked = time.monotonic()
        raw = _fetch(url_for(name, days))
    return parse(raw, symbol.upper(), name)


def stamp(when: datetime | None) -> str | None:
    return when.isoformat() if when else None
