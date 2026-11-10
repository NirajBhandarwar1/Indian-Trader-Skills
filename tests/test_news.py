"""The news monitor, on what the exchange and the news feed really served.

EVERY ITEM HERE IS REAL: a week of market-wide announcements (2,223 filings), three days of them in
the folder --offline reads, and the headlines and quotes for five companies, all saved by
capture.py. The known weaknesses of keyword tags show up in real filings, and the tests record
them rather than hiding them.
"""
import gzip
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _load import FIXTURES, fixture, has, skill                  # noqa: E402
from nsetools import clock, headlines                            # noqa: E402

news = skill("nse-news", "news")
WEEK = "exchange/announcements.json.gz"
SAVED = FIXTURES / "news"
NOW = datetime(2026, 10, 24, 14, 30, tzinfo=clock.IST)
WATCH = ["TCS", "HDFCBANK", "INFY", "SBIN", "ITC"]


def week_items():
    """The week's filings as the skill turns them into items."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "announcements.json").write_text(json.dumps(fixture(WEEK)),
                                                      encoding="utf-8")
        return news.announcements(NOW, 24 * 8, tmp)


def tagged(items, watch=()):
    return news.tag(items, list(watch), {})


def run(*args):
    """The command, offline, with a state file of its own. Returns (exit, json)."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        code = news.main(["--offline", str(SAVED), "--now", "2026-10-24 14:30", "--state",
                          str(state), "--json", *args])
        return code, state


@unittest.skipUnless(has(WEEK), "run capture.py")
class TheTagsOnAWeekOfRealFilings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = tagged(week_items())

    def find(self, phrase):
        for item in self.items:
            if phrase.lower() in f"{item['category']} {item['title']}".lower():
                return item
        self.fail(f"no real filing contained {phrase!r}")

    def test_it_is_a_week_of_real_filings(self):
        self.assertGreater(len(self.items), 1500)
        self.assertTrue(all(i["time"] is not None for i in self.items))

    def test_the_text_comes_from_attchmntText_not_the_company_name(self):
        rows = fixture(WEEK)
        sample = next(r for r in rows if (r.get("attchmntText") or "").strip())
        item = next(i for i in self.items if i["id"] == f"nse:{sample['seq_id']}")
        self.assertNotEqual(item["title"], sample["sm_name"])

    def test_every_tag_fires_on_at_least_one_real_item(self):
        """A tag that never matches real language is decoration. Headlines carry the macro and
        analyst language filings rarely use, so both feeds are read."""
        found = set(t for i in self.items for t in i["tags"])
        names = dict((r["symbol"], r["name"]) for r in json.loads(
            (SAVED / "equities.json").read_text(encoding="utf-8"))["data"])
        for symbol in WATCH:
            got, *_ = headlines.for_company(symbol, names[symbol], 2, str(SAVED))
            found |= set(t for i in tagged(got) for t in i["tags"])
        for label, _ in news.TAGS:
            if label == "macro":
                continue          # measured: no macro word in this capture's titles or filings
            self.assertIn(label, found, f"{label} matched nothing real")

    def test_most_filings_carry_no_tag(self):
        bare = sum(1 for i in self.items if not i["tags"])
        self.assertGreater(bare, len(self.items) * 0.3)

    def test_an_order_win(self):
        self.assertIn("order", self.find("bags")["tags"])

    def test_a_result(self):
        self.assertIn("earnings", self.find("financial results for the quarter")["tags"])

    def test_a_fundraise_is_capital(self):
        self.assertIn("capital", self.find("qip")["tags"])

    def test_a_person_leaving(self):
        self.assertIn("people", self.find("resignation of director")["tags"])

    def test_a_credit_rating(self):
        self.assertIn("rating", self.find("credit rating")["tags"])

    def test_an_insolvency(self):
        self.assertIn("legal", self.find("nclt")["tags"])

    def test_an_order_passed_against_a_company_is_legal_not_an_order_win(self):
        """The first version tagged "Action(s) taken or orders passed" like an order WON."""
        item = self.find("Action(s) taken or orders passed")
        self.assertIn("legal", item["tags"])
        self.assertNotIn("order", item["tags"])


@unittest.skipUnless(has(WEEK), "run capture.py")
class WhatMatters(unittest.TestCase):
    """"!" goes on the Indian counterparts of the 8-K items the US pack marks."""

    @classmethod
    def setUpClass(cls):
        cls.items = tagged(week_items())

    def test_every_important_category_is_one_the_exchange_really_uses(self):
        """Read off 15,294 real announcements over thirty days. A typo here would silently mark
        nothing; this checks the ones present in the saved week are spelled as the exchange
        spells them."""
        seen = {i["category"] for i in self.items}
        present = news.IMPORTANT & seen
        self.assertGreaterEqual(len(present), 5)

    def test_a_resignation_is_marked(self):
        marked = [i for i in self.items if i["category"] == "Resignation of Director/KMP/SMP"]
        self.assertTrue(marked)
        self.assertTrue(all(i["important"] for i in marked))

    def test_a_board_meeting_about_results_is_marked_and_tagged_earnings(self):
        hits = [i for i in self.items if i["category"] == "Outcome of Board Meeting"
                and re.search(r"\bresults?\b", i["title"], re.I)]
        self.assertTrue(hits)
        self.assertTrue(all(i["important"] and "earnings" in i["tags"] for i in hits))

    def test_routine_paperwork_is_not_marked(self):
        routine = [i for i in self.items if i["category"] == "Trading Window"]
        self.assertTrue(routine)
        self.assertFalse(any(i["important"] for i in routine))

    def test_a_headline_is_never_marked(self):
        """"!" is for what the company filed. A headline is someone else's report of it."""
        names = {"INFY": "Infosys Limited"}
        got, *_ = headlines.for_company("INFY", names["INFY"], 2, str(SAVED))
        self.assertFalse(any(i["important"] for i in tagged(got, ["INFY"])))


@unittest.skipUnless((SAVED / "headlines-ITC.xml").exists(), "run capture.py")
class HeadlinesThatNameTheCompany(unittest.TestCase):
    def names(self):
        return {r["symbol"]: r["name"] for r in json.loads(
            (SAVED / "equities.json").read_text(encoding="utf-8"))["data"]}

    def test_every_kept_headline_names_the_company(self):
        for symbol, legal in self.names().items():
            got, *_ = headlines.for_company(symbol, legal, 2, str(SAVED))
            name = headlines.short_name(legal)
            for item in got:
                self.assertRegex(item["title"], re.compile(re.escape(name), re.I), item["title"])

    def test_headlines_that_do_not_name_it_are_counted_not_hidden(self):
        got, unnamed, pages, _ = headlines.for_company("HDFCBANK", self.names()["HDFCBANK"], 2,
                                                    str(SAVED))
        self.assertGreater(len(got) + unnamed + pages, len(got))

    def test_an_abbreviated_name_needs_a_word_that_ties_it_to_the_company(self):
        """ITC files as "ITC Limited". Its search brought back GST input-tax-credit stories."""
        got, unnamed, *_ = headlines.for_company("ITC", self.names()["ITC"], 2, str(SAVED))
        for item in got:
            self.assertRegex(item["title"], r"ITC(?:'s)?\s+(?i:" + headlines.TIES + r")\b|NSE\s*:\s*ITC")
        self.assertGreater(unnamed, 0)

    def test_the_publisher_is_kept_and_cut_from_the_title(self):
        got, *_ = headlines.for_company("INFY", self.names()["INFY"], 2, str(SAVED))
        self.assertTrue(got)
        for item in got:
            self.assertFalse(item["title"].endswith(f" - {item['source']}"))

    def test_quote_and_profile_pages_are_not_news(self):
        for title in ("HDFC Bank Limited (HDFCBANK.NS) stock price, news, quote and history",
                      "Bajaj Finance Limited Estimates – BSE:BAJFINANCE - TradingView",
                      "Infosys Limited (INFY) Stock Forecasts"):
            self.assertTrue(headlines.NOT_NEWS.search(title), title)
        self.assertFalse(headlines.NOT_NEWS.search(
            "HDFC Bank Shares Fall 2.3% After Q2 Results"))


@unittest.skipUnless((SAVED / "announcements.json.gz").exists(), "run capture.py")
class OnlyWhatIsNew(unittest.TestCase):
    def test_the_first_check_shows_items_and_the_second_shows_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            # --top wide enough for everything: an item cut by --top stays new, on purpose.
            args = ["--tickers", *WATCH, "--offline", str(SAVED), "--now", "2026-10-24 14:30",
                    "--state", str(state), "--no-moves", "--top", "100000"]
            self.assertEqual(news.main(args), 0)
            self.assertEqual(news.main(args), 1)          # nothing new
            self.assertEqual(news.main(args + ["--all"]), 1)   # shown again, still not new

    def test_a_dry_run_leaves_the_state_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            args = ["--tickers", "INFY", "--offline", str(SAVED), "--now", "2026-10-24 14:30",
                    "--state", str(state), "--no-moves", "--dry-run"]
            self.assertEqual(news.main(args), 0)
            self.assertFalse(state.exists())
            self.assertEqual(news.main(args), 0)

    def test_seen_items_are_forgotten_after_thirty_days(self):
        state = {"seen": {"old": (NOW - timedelta(days=31)).isoformat(),
                          "recent": (NOW - timedelta(days=2)).isoformat()}}
        news.remember([], state, NOW)
        self.assertEqual(set(state["seen"]), {"recent"})

    def test_the_window_is_hours_back_from_now_and_nothing_after_it(self):
        items = tagged(news.announcements(NOW, 72, str(SAVED)))
        shown = news.select(items, {}, NOW - timedelta(hours=24), 6)
        cutoff, ahead = NOW - timedelta(hours=30), NOW - timedelta(hours=24) + timedelta(minutes=10)
        self.assertTrue(shown)
        self.assertTrue(all(cutoff <= i["time"] <= ahead for i in shown))

    def test_newest_first(self):
        items = tagged(news.announcements(NOW, 72, str(SAVED)))
        shown = news.select(items, {}, NOW, 72)
        times = [i["time"] for i in shown]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_an_unreadable_date_is_unknown_never_now(self):
        """Stamping an old filing with this minute would make it look like breaking news."""
        self.assertIsNone(news._ist("sometime last week"))


@unittest.skipUnless((SAVED / "announcements.json.gz").exists(), "run capture.py")
class TheCommand(unittest.TestCase):
    def output(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            import contextlib
            import io
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = news.main(["--offline", str(SAVED), "--now", "2026-10-24 14:30",
                                  "--state", str(Path(tmp) / "s.json"), "--json", *args])
            return code, json.loads(buffer.getvalue())

    def test_only_the_watched_companies_and_what_names_them(self):
        code, got = self.output("--tickers", *WATCH)
        self.assertEqual(code, 0)
        for item in got["items"]:
            self.assertTrue(set(item["touches"]) & set(WATCH) or item["symbol"] in WATCH, item)

    def test_routine_filings_are_left_out_and_counted(self):
        _, quiet = self.output("--market")
        _, loud = self.output("--market", "--routine")
        self.assertGreater(quiet["routine_left_out"], 0)
        self.assertEqual(len(loud["items"]), len(quiet["items"]) + quiet["routine_left_out"])

    def test_the_price_line_is_flagged_at_the_threshold(self):
        _, got = self.output("--tickers", "SBIN", "--move-pct", "1")
        sbin = got["moves"][0]
        self.assertEqual(sbin["pct"], -1.18)          # 958.60 against 970.00
        self.assertTrue(sbin["moving"])
        _, got = self.output("--tickers", "SBIN")
        self.assertFalse(got["moves"][0]["moving"])

    def test_no_headlines_means_only_filings(self):
        _, got = self.output("--tickers", *WATCH, "--no-headlines")
        self.assertTrue(all(i["kind"] == "filing" for i in got["items"]))

    def test_a_tag_filter(self):
        _, got = self.output("--tickers", *WATCH, "--tag", "earnings")
        self.assertTrue(got["items"])
        self.assertTrue(all("earnings" in i["tags"] for i in got["items"]))

    def test_nothing_to_watch_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(news.main(["--watchlist", str(Path(tmp) / "none.json"), "--plan",
                                        str(Path(tmp) / "none.json"), "--portfolio",
                                        str(Path(tmp) / "none.json"), "--offline",
                                        str(SAVED)]), 2)

    def test_the_watchlist_plan_and_portfolio_supply_the_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "w.json").write_text(json.dumps(fixture("chain/watchlist.json")),
                                         encoding="utf-8")
            (home / "p.json").write_text(json.dumps(fixture("chain/plan.json")), encoding="utf-8")
            (home / "f.json").write_text(json.dumps({"positions": [{"symbol": "TCS"}]}),
                                         encoding="utf-8")
            got = news.watch_from_files(home / "f.json", home / "w.json", home / "p.json")
            self.assertEqual(got, ["SBIN", "SUZLON", "ITC", "TCS"])


def quietly(args, cwd=None):
    """news.main in text mode, its printout captured. Returns (exit, text)."""
    import contextlib
    import io
    import os
    buffer = io.StringIO()
    here = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = news.main(args)
    finally:
        os.chdir(here)
    return code, buffer.getvalue()


def printed_items(text):
    return [line for line in text.splitlines() if re.match(r"[! ] \d{4}-\d\d-\d\d \d\d:\d\d IST ",
                                                           line)]


def real_title(feed, start):
    """A headline title exactly as the saved feed served it, publisher cut."""
    raw = (SAVED / feed).read_text(encoding="utf-8")
    for title in re.findall(r"<title>([^<]*)</title>", raw):
        if title.startswith(start):
            return re.sub(r" - [^-]+$", "", title).replace("&amp;", "&")
    raise AssertionError(f"no saved title starts {start!r}")


def headline(symbol, title):
    return {"id": "news:" + title, "kind": "headline", "symbol": symbol, "title": title,
            "link": "", "source": "news", "time": NOW, "category": None}


@unittest.skipUnless((SAVED / "announcements.json.gz").exists(), "run capture.py")
class OnlyWhatWasPrintedIsSeen(unittest.TestCase):
    """The US pack prints every item it remembers. Here --top and the routine filter hide some,
    and marking those seen meant the next check could never show them."""

    def base(self, state):
        return ["--offline", str(SAVED), "--now", "2026-10-24 14:30", "--state", str(state),
                "--no-moves"]

    def test_items_cut_by_top_stay_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.base(Path(tmp) / "s.json") + ["--tickers", *WATCH]
            _, everything = quietly(args + ["--dry-run", "--top", "1000"])
            total = len(printed_items(everything))
            self.assertGreater(total, 5)
            # A "!" item further down is printed whatever --top says (TheFourthPass).
            marked = sum(1 for line in printed_items(everything)[5:] if line.startswith("!"))
            code, first = quietly(args + ["--top", "5"])
            self.assertEqual((code, len(printed_items(first))), (0, 5 + marked))
            code, second = quietly(args + ["--top", "1000"])
            self.assertEqual(code, 0)
            self.assertEqual(len(printed_items(second)), total - 5 - marked)
            self.assertFalse(set(printed_items(first)) & set(printed_items(second)))

    def test_a_routine_filing_left_out_is_shown_by_routine_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.base(Path(tmp) / "s.json") + ["--tickers", "HDFCBANK", "--no-headlines",
                                                     "--hours", "200"]
            _, first = quietly(args)
            self.assertIn("1 routine compliance filing(s) left out", first)
            self.assertNotIn("Copy of Newspaper Publication", first)
            code, later = quietly(args + ["--routine"])
            self.assertEqual(code, 0)
            self.assertIn("Copy of Newspaper Publication", later)
            self.assertEqual(len(printed_items(later)), 1)

    def test_a_market_sweep_keeps_its_own_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            common = ["--offline", str(SAVED), "--now", "2026-10-24 14:30", "--no-moves",
                      "--hours", "200", "--top", "100000"]
            code, _ = quietly(common + ["--market"], cwd=tmp)
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "nse/state/news-market.json").exists())
            self.assertFalse((Path(tmp) / "nse/state/news.json").exists())
            mine = common + ["--tickers", *WATCH, "--no-headlines"]
            _, fresh = quietly(mine + ["--state", str(Path(tmp) / "fresh.json")], cwd=tmp)
            code, after = quietly(mine, cwd=tmp)
            self.assertEqual(code, 0)
            self.assertTrue(printed_items(fresh))
            self.assertEqual(printed_items(after), printed_items(fresh))

    def test_state_still_overrides_the_market_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "mine.json"
            quietly(["--offline", str(SAVED), "--now", "2026-10-24 14:30", "--market",
                     "--state", str(state)], cwd=tmp)
            self.assertTrue(state.exists())
            self.assertFalse((Path(tmp) / "nse/state/news-market.json").exists())


@unittest.skipUnless((SAVED / "announcements.json.gz").exists(), "run capture.py")
class TwoCompaniesFilingTheSameWords(unittest.TestCase):
    """Measured in the saved days: KICL and KSL filed the same text the same day, and so did
    EDELWEISS and RITCO. The US pack's title names the company, so it never met this."""

    def test_both_are_kept_each_under_its_own_link(self):
        items = [i for i in news.dedupe(news.tag(news.announcements(NOW, 200, str(SAVED)),
                                                 ["KICL", "KSL"], {}))
                 if i["symbol"] in ("KICL", "KSL")
                 and i["category"] == "Copy of Newspaper Publication"]
        self.assertEqual(sorted(i["symbol"] for i in items), ["KICL", "KSL"])
        for item in items:
            self.assertEqual(item["touches"], [item["symbol"]])
            self.assertIn(item["symbol"], item["link"])

    def test_the_market_sweep_keeps_every_company(self):
        items = news.dedupe(news.tag(news.announcements(NOW, 200, str(SAVED)), [], {}))
        symbols = {i["symbol"] for i in items}
        self.assertTrue({"KICL", "KSL", "EDELWEISS", "RITCO"} <= symbols)

    def test_the_same_story_in_two_feeds_is_still_one(self):
        title = real_title("headlines-INFY.xml", "AI contributes over 10% of TCS")
        a, b = headline("INFY", title), headline("TCS", title)
        b["link"] = a["link"] = "https://news.google.com/same"
        b["id"] = "news:other"
        got = news.dedupe(news.tag([a, b], ["INFY", "TCS"], {}))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["touches"], ["INFY", "TCS"])


@unittest.skipUnless((SAVED / "headlines-SBIN.xml").exists(), "run capture.py")
class ProfilePagesJudgedWithTheirPublisher(unittest.TestCase):
    def test_the_tradingview_and_marketbeat_pages_for_sbi_are_left_out(self):
        got, _, pages, _ = headlines.for_company("SBIN", "State Bank of India", 2, str(SAVED))
        titles = [i["title"] for i in got]
        self.assertFalse([t for t in titles if "Estimates" in t], titles)
        self.assertEqual(pages, 2)

    def test_real_news_from_tradingview_is_kept(self):
        got, *_ = headlines.for_company("HDFCBANK", "HDFC Bank Limited", 2, str(SAVED))
        self.assertIn("HDFC Bank Probes Two Senior Executives Over Confidential Data Leak",
                      [i["title"] for i in got])

    def test_a_headline_that_beats_estimates_is_news(self):
        got, *_ = headlines.for_company("INFY", "Infosys Limited", 2, str(SAVED))
        self.assertTrue(any(i["title"].startswith("Infosys (INFY) Q2 Earnings: How Key Metrics")
                            for i in got))


@unittest.skipUnless((SAVED / "announcements.json.gz").exists(), "run capture.py")
class FormsSeparatedByCommas(unittest.TestCase):
    def filings(self, *forms):
        _, got = TheCommand.output(self, "--tickers", "INFY", "--no-headlines", "--hours", "200",
                                   "--no-moves", *forms)
        return got["items"]

    def test_comma_separated_like_the_us_pack(self):
        both = self.filings("--forms", "Outcome of Board Meeting,Record Date")
        self.assertEqual(sorted(i["category"] for i in both),
                         ["Outcome of Board Meeting", "Record Date"])
        self.assertEqual(len(both), len(self.filings("--forms", "Outcome of Board Meeting"))
                         + len(self.filings("--forms", "Record Date")))

    def test_the_comma_inside_the_exchanges_own_category_is_not_a_split(self):
        _, got = TheCommand.output(self, "--market", "--routine", "--hours", "200", "--forms",
                                   "Certificate under SEBI (Depositories and Participants) "
                                   "Regulations, 2018")
        self.assertTrue(got["items"])
        self.assertEqual({i["category"] for i in got["items"]},
                         {"Certificate under SEBI (Depositories and Participants) Regulations, "
                          "2018"})


@unittest.skipUnless(has(WEEK), "run capture.py")
class TagsThatMeanWhatTheySay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = tagged(week_items())

    def test_citing_the_sebi_rule_a_filing_is_made_under_is_not_regulatory(self):
        cited = [i for i in self.items if "Regulation 30 of SEBI" in i["title"]]
        self.assertGreater(len(cited), 3)
        for item in cited:
            if "regulatory" in item["tags"]:
                self.assertRegex(item["title"], r"(?i)\b(?:RBI|licen[cs]e|USFDA|FDA|CDSCO|"
                                 r"regulator|ministry)\b")

    def test_a_sebi_order_is_regulatory(self):
        """A real filing with one fact changed: the order is SEBI's."""
        real = next(i for i in self.items if i["category"] == "Action(s) initiated or orders passed")
        for text in (real["title"] + " SEBI order dated October 22, 2026",
                     real["title"] + " Show cause notice issued by SEBI"):
            item = dict(real, title=text)
            self.assertIn("regulatory", tagged([item])[0]["tags"], text)
        notice = dict(real, title="Notice under Regulation 30 of SEBI (LODR) Regulations, 2015",
                      category="General Updates")
        self.assertNotIn("regulatory", tagged([notice])[0]["tags"])

    def test_a_company_named_cyber_is_not_a_risk(self):
        named = [i for i in self.items if re.search(r"\bCyber(?:tech| Media)", i["title"])]
        self.assertTrue(named)
        for item in named:
            if "risk" in item["tags"]:
                self.assertRegex(f"{item['category']} {item['title']}",
                                 r"(?i)default|insolvency|fraud|bankrupt|breach|strike|lockout|"
                                 r"disruption|forensic|closure of operations|recall")

    def test_a_cyberattack_is_a_risk(self):
        title = real_title("headlines-SBIN.xml", "Cyberattack hits India")
        self.assertIn("risk", tagged([headline("SBIN", title)])[0]["tags"])


@unittest.skipUnless((SAVED / "headlines-TCS.xml").exists(), "run capture.py")
class TheSymbolTouchesOnlyInItsListedShapes(unittest.TestCase):
    def touches(self, feed, start, watch):
        title = real_title(feed, start)
        return news.tag([headline("OTHER", title)], watch, {})[0]["touches"]

    def test_nse_colon_symbol(self):
        self.assertEqual(self.touches("headlines-TCS.xml", "TCS (NSE:TCS) Gains", ["TCS"]),
                         ["TCS"])

    def test_symbol_in_brackets(self):
        self.assertEqual(self.touches("headlines-INFY.xml", "What Infosys (INFY) Said",
                                      ["INFY"]), ["INFY"])

    def test_a_bare_symbol_is_not_enough(self):
        self.assertEqual(self.touches("headlines-INFY.xml", "TCS Vs Infosys Vs HCLTech", ["TCS"]),
                         [])

    def test_an_abbreviation_being_spelled_out_is_not_the_stock(self):
        self.assertEqual(self.touches("headlines-ITC.xml", "Powering Up, Taxed Down", ["ITC"]),
                         [])

    def test_lower_case_is_not_the_symbol(self):
        self.assertFalse(news.names_symbol("Input credit (itc) rules", "ITC"))
        self.assertFalse(news.names_symbol("nse:tcs", "TCS"))


class BothSourcesOff(unittest.TestCase):
    def test_no_filings_and_no_headlines_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, text = quietly(["--offline", str(SAVED), "--now", "2026-10-24 14:30",
                                  "--state", str(Path(tmp) / "s.json"), "--tickers", "TCS",
                                  "--no-filings", "--no-headlines"])
            self.assertEqual((code, text), (2, ""))
            self.assertFalse((Path(tmp) / "s.json").exists())


def offline_copy(tmp, change=None):
    """The saved news folder copied into `tmp`, with `change(folder)` applied: one fact altered."""
    import shutil
    folder = Path(tmp) / "saved"
    shutil.copytree(SAVED, folder)
    if change:
        change(folder)
    return str(folder)


def base(state, folder=None):
    return ["--offline", folder or str(SAVED), "--now", "2026-10-24 14:30", "--state", str(state)]


@unittest.skipUnless((SAVED / "announcements.json.gz").exists(), "run capture.py")
class TheFourthPass(unittest.TestCase):
    """Found by a side-by-side pass against the US pack, and closed. Each on real saved answers."""

    def test_a_symbol_can_be_given_bare_as_other_skills_write_it(self):
        """/nse-delivery, /nse-insider and /nse-scan all say "/nse-news SYM"; that was refused."""
        with tempfile.TemporaryDirectory() as tmp:
            code, text = quietly(["SBIN", *base(Path(tmp) / "s.json"), "--dry-run"])
            _, same = quietly(["--tickers", "SBIN", *base(Path(tmp) / "s.json"), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("News for SBIN,", text)
        self.assertEqual(text, same)

    def test_a_watchlist_hit_written_with_ticker_like_the_us_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = fixture("chain/watchlist.json")
            for hit in saved["hits"]:
                hit["ticker"] = hit.pop("symbol")
            (Path(tmp) / "w.json").write_text(json.dumps(saved), encoding="utf-8")
            got = news.watch_from_files(Path(tmp) / "none", Path(tmp) / "w.json",
                                        Path(tmp) / "none")
        self.assertEqual(got, ["SBIN", "SUZLON", "ITC"])

    def test_a_damaged_watchlist_is_said_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = json.dumps(fixture("chain/watchlist.json"))
            (Path(tmp) / "w.json").write_text(text[:-5], encoding="utf-8")   # cut short
            (Path(tmp) / "f.json").write_text(json.dumps({"positions": [{"symbol": "INFY"}, 5]}),
                                              encoding="utf-8")
            code, said = quietly(["--watchlist", str(Path(tmp) / "w.json"), "--plan",
                                  str(Path(tmp) / "none"), "--portfolio", str(Path(tmp) / "f.json"),
                                  *base(Path(tmp) / "s.json"), "--dry-run", "--json"])
        self.assertEqual(code, 0)
        got = json.loads(said)
        self.assertTrue(any("w.json could not be read" in n for n in got["notes"]), got["notes"])
        self.assertTrue(all(i["symbol"] == "INFY" or "INFY" in i["touches"] for i in got["items"]))

    def test_a_damaged_saved_feed_is_a_note_not_a_traceback(self):
        def damage(folder):
            (folder / "announcements.json.gz").unlink()
            raw = gzip.decompress((SAVED / "announcements.json.gz").read_bytes())
            (folder / "announcements.json").write_bytes(raw[: len(raw) // 2])
        with tempfile.TemporaryDirectory() as tmp:
            code, text = quietly(["INFY", *base(Path(tmp) / "s.json", offline_copy(tmp, damage)),
                                  "--dry-run"])
        self.assertEqual(code, 0)              # the headlines were still read
        self.assertIn("note: the exchange's announcements:", text)
        self.assertIn("could not be read", text)

    def test_a_damaged_equity_list_does_not_blame_the_symbol(self):
        def damage(folder):
            (folder / "equities.json").write_text("{", encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            code, text = quietly(["INFY", *base(Path(tmp) / "s.json", offline_copy(tmp, damage)),
                                  "--dry-run", "--no-moves"])
        self.assertEqual(code, 0)              # the filings were still read
        self.assertIn("the equity list:", text)
        self.assertNotIn("check the symbol", text)

    def test_an_unknown_symbol_is_said_even_with_headlines_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, text = quietly(["ZZZZ", "INFY", "--no-headlines", *base(Path(tmp) / "s.json"),
                                  "--dry-run", "--no-moves"])
        self.assertEqual(code, 0)
        self.assertIn("ZZZZ is not in the exchange's EQ-series equity list and filed nothing in "
                      "the window", text)

    def test_only_unknown_symbols_is_not_nothing_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, text = quietly(["ZZZZ", "--no-headlines", *base(Path(tmp) / "s.json"),
                                  "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("NOT a sign of no news", text)
        self.assertNotIn("Nothing new", text)

    def test_reading_nothing_exits_two_not_one(self):
        """Exit 1 tells a /loop "nothing new"; when nothing could be read that is false."""
        def damage(folder):
            (folder / "headlines-INFY.xml").write_text("<rss><channel><item>", encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            code, text = quietly(["INFY", "--no-filings", "--no-moves",
                                  *base(Path(tmp) / "s.json", offline_copy(tmp, damage))])
        self.assertEqual(code, 2)
        self.assertIn("headlines for INFY: the news feed answered something unreadable", text)

    def test_a_missing_saved_headline_file_is_said(self):
        def remove(folder):
            (folder / "headlines-TCS.xml").unlink()
        with tempfile.TemporaryDirectory() as tmp:
            _, text = quietly(["TCS", "INFY", *base(Path(tmp) / "s.json", offline_copy(tmp, remove)),
                               "--dry-run", "--no-moves"])
        self.assertIn("headlines for TCS: TCS: no saved headlines", text)

    def test_flags_that_would_read_nothing_or_be_ignored_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "s.json"
            for extra in (["--market", "--tickers", "TCS"], ["--market", "SBIN"],
                          ["--market", "--no-filings"], ["TCS", "--hours", "-5"],
                          ["TCS", "--days", "0"], ["TCS", "--top", "-3"], ["TCS", "--top", "0"],
                          ["TCS", "--move-pct", "-1"], ["TCS", "--no-filings", "--forms", "Record"],
                          ["TCS", "--hours", "6", "--days", "2"]):
                code, text = quietly([*base(state), *extra])
                self.assertEqual((code, text), (2, ""), extra)
            self.assertFalse(state.exists())

    def test_a_bad_setting_is_refused_not_a_traceback_or_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            for setting in ({"news_hours": "abc"}, {"news_hours": 0}, {"news_move_pct": "x"},
                            {"news_move_pct": -2}, {"news_hours": float("nan")}):
                conf = Path(tmp) / "c.json"
                conf.write_text(json.dumps(setting), encoding="utf-8")
                code, _ = quietly(["TCS", *base(Path(tmp) / "s.json"), "--config", str(conf)])
                self.assertEqual(code, 2, setting)
            conf.write_text(json.dumps({"news_move_pct": 0}), encoding="utf-8")
            code, text = quietly(["SBIN", *base(Path(tmp) / "s.json"), "--config", str(conf),
                                  "--dry-run"])
            self.assertEqual(code, 0)
            self.assertRegex(text, r"SBIN .*vs the previous close .*MOVING")   # 0 means every move

    def test_a_damaged_state_file_is_refused_and_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "good.json"
            quietly(["INFY", *base(first), "--no-moves"])
            real = first.read_text(encoding="utf-8")
            for damaged in (real[:-10], json.dumps([json.loads(real)]),
                            json.dumps({"seen": list(json.loads(real)["seen"])}),
                            json.dumps({"seen": {k: 1 for k in json.loads(real)["seen"]}})):
                state = Path(tmp) / "bad.json"
                state.write_text(damaged, encoding="utf-8")
                code, text = quietly(["INFY", *base(state), "--no-moves"])
                self.assertEqual((code, text), (2, ""))
                self.assertEqual(state.read_text(encoding="utf-8"), damaged)

    def test_a_state_that_cannot_be_written_is_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("", encoding="utf-8")
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = news.main(["INFY", *base(blocker / "s.json"), "--no-moves"])
        self.assertEqual(code, 2)
        self.assertIn("could not be written", err.getvalue())

    def test_now_in_iso_shape_like_the_us_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ["INFY", "--offline", str(SAVED), "--state", str(Path(tmp) / "s.json"),
                    "--dry-run"]
            a = quietly(args + ["--now", "2026-10-24T14:30"])
            b = quietly(args + ["--now", "2026-10-24 14:30"])
        self.assertEqual(a, b)

    def quote_folder(self, tmp, **meta):
        def change(folder):
            body = json.loads((SAVED / "SBIN.json").read_text(encoding="utf-8"))
            for key, value in meta.items():
                if value is None:
                    body["chart"]["result"][0]["meta"].pop(key, None)
                else:
                    body["chart"]["result"][0]["meta"][key] = value
            (folder / "SBIN.json").write_text(json.dumps(body), encoding="utf-8")
        return offline_copy(tmp, change)

    def test_a_nan_price_is_never_printed(self):
        for meta in ({"regularMarketPrice": float("nan")}, {"regularMarketPrice": 0},
                     {"previousClose": float("nan"), "chartPreviousClose": float("nan")}):
            with tempfile.TemporaryDirectory() as tmp:
                _, text = quietly(["SBIN", *base(Path(tmp) / "s.json",
                                                self.quote_folder(tmp, **meta)), "--dry-run"])
            self.assertNotIn("Rs nan", text)
            self.assertNotIn("Rs 0.00", text)
            self.assertIn("note: price for SBIN:", text)

    def test_a_quote_with_no_time_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, text = quietly(["SBIN", *base(Path(tmp) / "s.json",
                                            self.quote_folder(tmp, regularMarketTime=None)),
                               "--dry-run"])
        self.assertIn("SBIN        Rs 958.60 -1.18% vs the previous close Rs 970.00 "
                      "(the feed gave no time for this quote)", text)

    def test_the_quote_carries_its_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, text = quietly(["SBIN", *base(Path(tmp) / "s.json"), "--dry-run"])
        self.assertIn("(quote of 23 Oct 2026 15:28 IST)", text)

    def test_a_cut_title_says_it_was_cut(self):
        """The real INFY auditors' filing is 161 characters; the line holds 160."""
        with tempfile.TemporaryDirectory() as tmp:
            _, text = quietly(["INFY", *base(Path(tmp) / "s.json"), "--dry-run", "--no-moves",
                               "--no-headlines"])
        line = next(l for l in printed_items(text) if "Report of auditors" in l)
        self.assertTrue(line.endswith("..."), line)

    def test_a_marked_filing_is_never_cut_by_top(self):
        """The default run of the five stocks had Infosys' results filing at number 105 of 154
        (49 of 87 once a one-word name needed a tie); --top 20 puts it well past the cut."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "s.json"
            _, every = quietly(["--tickers", *WATCH, *base(state), "--dry-run", "--top", "1000"])
            total = printed_items(every)
            code, text = quietly(["--tickers", *WATCH, *base(state), "--top", "20"])
            lines = printed_items(text)
            self.assertEqual(code, 0)
            marked = [l for l in total[20:] if l.startswith("!")]
            self.assertTrue(any("INFY" in l and "Financial Results" in l for l in marked), marked)
            self.assertEqual(lines, total[:20] + marked)
            self.assertIn(f"... and {len(total) - len(lines)} more", text)
            # Printed, so remembered: the next run does not show it again.
            _, again = quietly(["--tickers", *WATCH, *base(state), "--top", "1000"])
            self.assertFalse([l for l in printed_items(again) if l.startswith("!")])

    def test_the_routine_count_is_in_the_json_notes_too(self):
        _, got = TheCommand.output(self, "--market")
        self.assertGreater(got["routine_left_out"], 0)
        self.assertTrue(any(n.startswith(f"{got['routine_left_out']} routine") for n in
                            got["notes"]))
        self.assertFalse(got["read_nothing"])

    def test_headline_searches_are_paced(self):
        from unittest import mock
        raw = (SAVED / "headlines-INFY.xml").read_bytes()
        waits = []
        with mock.patch.object(headlines, "_fetch", lambda url: raw), \
                mock.patch.object(headlines.time, "sleep", waits.append):
            headlines._last_asked = 0.0
            headlines.for_company("INFY", "Infosys Limited", 2)
            headlines.for_company("INFY", "Infosys Limited", 2)
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 0)

    def test_quotes_are_paced(self):
        from unittest import mock
        real = news.quotes.quote
        waits = []
        with mock.patch.object(news.quotes, "quote",
                               lambda s, offline=None: real(s, offline=str(SAVED))), \
                mock.patch.object(news.time, "sleep", waits.append):
            got = news.moves(["SBIN", "INFY", "TCS"], None, [])
        self.assertEqual([m["symbol"] for m in got], ["SBIN", "INFY", "TCS"])
        self.assertEqual(waits, [news.quotes.PAUSE] * 2)


@unittest.skipUnless((SAVED / "headlines-INFY.xml").exists(), "run capture.py")
class AOneWordNameNeedsATie(unittest.TestCase):
    """Bosch Limited files as "Bosch" and Siemens Limited as "Siemens": a headline naming either
    can be about the foreign parent. Infosys is the one-word name in the saved feeds, and every one
    of its 98 kept headlines was about Infosys, so this measures what the rule costs."""

    def test_on_the_saved_infosys_search_every_kept_headline_carries_a_tie(self):
        got, unnamed, pages, untied = headlines.for_company("INFY", "Infosys Limited", 2,
                                                            str(SAVED))
        self.assertEqual((len(got), unnamed, pages, untied), (29, 0, 2, 69))
        for item in got:
            self.assertTrue(headlines.INDIA_TIES.search(item["title"])
                            or re.search(r"Infosys\s+(?:Ltd|Limited|India)\b|India.s Infosys|"
                                         r"\(INFY\)|NSE\s*:\s*INFY", item["title"], re.I),
                            item["title"])

    def test_the_same_words_about_a_foreign_parent_are_not_the_listed_company(self):
        """Real Infosys titles with the company changed to Siemens (Siemens AG abroad, Siemens
        Limited here): only the ones carrying rupees or the Indian market stay."""
        cut = real_title("headlines-INFY.xml", "Infosys Cuts Sales Forecast")
        kept = real_title("headlines-INFY.xml", "Infosys Q2 net jumps")
        for title, named in ((cut, False), (kept, True)):
            self.assertEqual(headlines.names_company(title.replace("Infosys", "Siemens"),
                                                     "SIEMENS", "Siemens"), named, title)
        self.assertTrue(headlines.names_company("Siemens Ltd bags order", "SIEMENS", "Siemens"))
        self.assertTrue(headlines.names_company("Siemens (SIEMENS) falls", "SIEMENS", "Siemens"))
        self.assertTrue(headlines.names_company(
            real_title("headlines-INFY.xml", "India’s Infosys cuts"), "INFY", "Infosys"))

    def test_a_one_word_name_written_as_a_common_word_is_not_the_company(self):
        title = "An eternal wait: Rs 500 crore project delayed"
        self.assertFalse(headlines.names_company(title, "ETERNAL", "ETERNAL"))
        self.assertTrue(headlines.names_company("Eternal Q2: Rs 500 crore loss", "ETERNAL",
                                                "ETERNAL"))

    def test_a_name_of_several_words_needs_no_tie_and_may_be_hyphenated(self):
        """As the US pack matches a name, its words joined by a space or a hyphen."""
        self.assertTrue(headlines.names_company("HDFC-Bank Probes Two Senior Executives",
                                                "HDFCBANK", "HDFC Bank"))

    def test_the_count_left_out_is_said_by_name(self):
        _, got = TheCommand.output(self, "INFY", "--no-moves", "--dry-run")
        note = next(n for n in got["notes"] if "headline(s) left out" in n)
        self.assertIn("69 named Infosys with nothing tying that one-word name", note)
        self.assertTrue(note.startswith("71 headline(s) left out"), note)

    def test_a_stock_is_touched_by_its_name_on_the_same_rule(self):
        """A title from another search names Infosys with no tie: it does not land on INFY."""
        title = real_title("headlines-INFY.xml", "TCS Vs Infosys Vs HCLTech")
        names = {"INFY": "Infosys Limited"}
        self.assertEqual(news.tag([headline("OTHER", title)], ["INFY"], names)[0]["touches"], [])
        tied = real_title("headlines-INFY.xml", "Infosys Q2 net jumps")
        self.assertEqual(news.tag([headline("OTHER", tied)], ["INFY"], names)[0]["touches"],
                         ["INFY"])


@unittest.skipUnless((SAVED / "announcements.json.gz").exists(), "run capture.py")
class TheFifthPass(unittest.TestCase):
    """Found by a fresh side-by-side pass against the US pack, and closed."""

    def watched(self, text, where="watchlist"):
        with tempfile.TemporaryDirectory() as tmp:
            files = {k: Path(tmp) / f"{k}.json" for k in ("watchlist", "plan", "portfolio")}
            files[where].write_text(text, encoding="utf-8")
            problems = []
            got = news.watch_from_files(files["portfolio"], files["watchlist"], files["plan"],
                                        problems)
        return got, problems

    def test_a_watch_file_in_the_wrong_shape_is_said_never_spelled_out(self):
        """A watchlist holding "TCS" was watched as T, C and S, and one holding 5 crashed."""
        for text, where in (('"TCS"', "watchlist"), ("5", "watchlist"),
                            ('{"hits": "TCS"}', "watchlist"), ('[{"symbol": "TCS"}]', "plan"),
                            ('{"positions": {"TCS": 1}}', "portfolio")):
            got, problems = self.watched(text, where)
            self.assertEqual(got, [], text)
            self.assertTrue(problems and "is not" in problems[0], (text, problems))
        got, problems = self.watched(json.dumps(fixture("chain/watchlist.json")))
        self.assertEqual((got, problems), (["SBIN", "SUZLON", "ITC"], []))

    def test_a_window_past_any_date_is_refused_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            for extra in (["--hours", "1e9"], ["--days", "1e12"]):
                code, text = quietly(["TCS", *base(Path(tmp) / "s.json"), *extra])
                self.assertEqual((code, text), (2, ""), extra)

    def test_an_empty_category_is_refused_not_nothing_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            for empty in ("", ",", " , "):
                code, text = quietly(["INFY", *base(Path(tmp) / "s.json"), "--category", empty])
                self.assertEqual((code, text), (2, ""), empty)

    def test_a_window_longer_than_the_memory_says_so(self):
        _, got = TheCommand.output(self, "INFY", "--days", "31", "--no-moves", "--dry-run")
        self.assertTrue(any("longer than the 30 days" in n for n in got["notes"]))
        _, got = TheCommand.output(self, "INFY", "--days", "30", "--no-moves", "--dry-run")
        self.assertFalse(any("longer than the 30 days" in n for n in got["notes"]))

    def test_with_headlines_off_a_lost_equity_list_says_symbols_are_unchecked(self):
        def damage(folder):
            (folder / "equities.json").unlink()
        with tempfile.TemporaryDirectory() as tmp:
            code, text = quietly(["ZZZZ", "INFY", "--no-headlines", "--no-moves", "--dry-run",
                                  *base(Path(tmp) / "s.json", offline_copy(tmp, damage))])
        self.assertEqual(code, 0)
        self.assertIn("whether each symbol is a listed company is not checked", text)

    def test_a_category_filter_asks_for_no_headlines(self):
        """It keeps filings only, so a headline search would be asked for and thrown away."""
        from unittest import mock
        with mock.patch.object(news.headlines, "for_company",
                               side_effect=AssertionError("asked")):
            code, got = TheCommand.output(self, "INFY", "--no-moves", "--hours", "200",
                                          "--category", "Record Date")
        self.assertEqual(code, 0)
        self.assertEqual([i["category"] for i in got["items"]], ["Record Date"])

    def test_one_story_from_two_searches_is_one_item(self):
        """The US pack merges the same title the same day across tickers' feeds."""
        title = real_title("headlines-INFY.xml", "AI contributes over 10% of TCS")
        a, b = headline("INFY", title), headline("TCS", title)
        b["id"], b["link"] = "news:other", "https://news.google.com/other"
        got = news.dedupe(news.tag([a, b], ["INFY", "TCS"], {}))
        self.assertEqual([i["touches"] for i in got], [["INFY", "TCS"]])

    def test_a_packed_saved_quote_is_read(self):
        def pack(folder):
            raw = (folder / "SBIN.json").read_bytes()
            (folder / "SBIN.json").unlink()
            (folder / "SBIN.json.gz").write_bytes(gzip.compress(raw))
        with tempfile.TemporaryDirectory() as tmp:
            _, text = quietly(["SBIN", *base(Path(tmp) / "s.json", offline_copy(tmp, pack)),
                               "--dry-run"])
        self.assertIn("SBIN        Rs 958.60 -1.18% vs the previous close Rs 970.00", text)

    def test_a_time_unknown_line_keeps_the_columns(self):
        real = news.announcements(NOW, 72, str(SAVED))[0]
        dated = news.render(tagged([dict(real)]), [], NOW, [], 3.0, 0, 60)[0]
        undated = news.render(tagged([dict(real, time=None)]), [], NOW, [], 3.0, 0, 60)[0]
        self.assertEqual(dated.index(real["symbol"]), undated.index(real["symbol"]))
        self.assertIn("time unknown", undated)

    def test_the_report_ends_saying_it_is_not_advice(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, text = quietly(["INFY", *base(Path(tmp) / "s.json"), "--dry-run", "--no-moves"])
        self.assertIn("Not financial advice", text.splitlines()[-1])

    def test_the_hidden_mine_flag_is_gone(self):
        """It was accepted and did nothing."""
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            news.main(["--mine", "--offline", str(SAVED)])

    def test_the_state_is_written_whole_with_nothing_left_beside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "s.json"
            code, _ = quietly(["INFY", *base(state), "--no-moves"])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(state.read_text(encoding="utf-8"))["seen"])
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["s.json"])

    def test_a_clock_pinned_in_the_past_searches_back_to_its_window(self):
        """The live search counts days back from the real today: pinned ten days ago with a
        two-day window, a two-day search missed the window and said nothing."""
        from unittest import mock
        asked = []
        raw = (SAVED / "headlines-INFY.xml").read_bytes()

        def search(symbol, name, days, offline=None):
            asked.append(days)
            return news.headlines.parse(raw, symbol, news.headlines.short_name(name))
        pinned = clock.now() - timedelta(days=10)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(news, "names_for", lambda s, o: ({"INFY": "Infosys Limited"},
                                                                   None)), \
                mock.patch.object(news, "announcements", lambda *a: []), \
                mock.patch.object(news.headlines, "for_company", search):
            code, _ = quietly(["INFY", "--no-moves", "--dry-run", "--state",
                               str(Path(tmp) / "s.json"), "--now",
                               pinned.strftime("%Y-%m-%d %H:%M")])
        self.assertIn(code, (0, 1))
        self.assertEqual(len(asked), 1)
        self.assertGreaterEqual(asked[0], 12)

    def test_an_exchange_answer_that_is_not_a_list_is_not_an_empty_market(self):
        """Read as [], an error answer printed "Nothing new." for the whole market."""
        from unittest import mock
        with mock.patch.object(news.nse, "fetch_json", lambda *a, **k: {"error": "blocked"}):
            with self.assertRaises(news.NotPublished):
                news.announcements(NOW, 48, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
