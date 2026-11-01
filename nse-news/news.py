#!/usr/bin/env python3
"""Corporate announcements from NSE: the fastest public signal the exchange carries.

This is India's equivalent of an 8-K. A result, an order win, a resignation, a fundraise or a
regulatory action all arrive here first, timestamped to the minute.

Needs no third-party packages and no account.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nsetools import nse, show                                 # noqa: E402
from nsetools.http import NotPublished                          # noqa: E402

# Words that mark an announcement worth reading before the others. Deliberately short: a long list
# of keywords becomes a filter that hides things, and the point of this skill is to show what was
# filed rather than to decide what matters.
LOUD = {
    "result": ("result", "financial result", "earnings"),
    "order": ("order", "contract", "letter of intent", "loi", "bags", "wins"),
    "fundraise": ("fund rais", "qip", "preferential", "rights issue", "debenture", "ncd"),
    "payout": ("dividend", "bonus", "split", "buy back", "buyback"),
    "people": ("resignation", "cessation", "appointment", "director"),
    "deal": ("acquisition", "acquire", "merger", "amalgamation", "divest", "stake sale"),
    "trouble": ("default", "insolvency", "nclt", "penalty", "show cause", "fraud", "resignation of auditor"),
    "ratings": ("rating", "credit rating"),
}

TAG_WIDTH = 10


def classify(text: str) -> str:
    low = (text or "").lower()
    for tag, words in LOUD.items():
        if any(w in low for w in words):
            return tag
    return ""


def tidy(raw: str, limit: int = 300) -> str:
    """Announcement text arrives with HTML, repeated whitespace and the company's own name in caps."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def when(row: dict) -> str:
    for key in ("an_dt", "sort_date", "exchdisstime", "dt"):
        raw = (row.get(key) or "").strip()
        if raw:
            for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                        "%d-%b-%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(raw[:19], fmt).strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    continue
            return raw[:16]
    return ""


def gather(symbol: str | None, days: int, only: str | None) -> list[dict]:
    rows = nse.announcements(symbol, days)
    out = []
    for row in rows:
        # desc is the exchange's own category for the filing -- "Updates", "Board Meeting",
        # "Financial Results". attchmntText is what the company actually wrote. sm_name is only the
        # company's name, which is what this showed on every row until it was checked against a
        # real answer: every line read "Reliance Industries Limited" and said nothing.
        kind = tidy(row.get("desc") or "", 28)
        said = tidy(row.get("attchmntText") or "", 400)
        tag = classify(f"{kind} {said}")
        if only and tag != only:
            continue
        out.append({
            "when": when(row),
            "symbol": (row.get("symbol") or "").upper(),
            "tag": tag,
            "kind": kind,
            "said": said or "(no text filed; the attachment carries it)",
            "industry": tidy(row.get("smIndustry") or "", 30),
            "attachment": row.get("attchmntFile") or "",
        })
    out.sort(key=lambda r: r["when"], reverse=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", help="one company; the whole market when left out")
    ap.add_argument("--days", type=int, default=3, help="how far back (default 3)")
    ap.add_argument("--tag", choices=sorted(LOUD), help="only this kind of announcement")
    ap.add_argument("--top", type=int, default=40, help="how many rows (default 40)")
    ap.add_argument("--json", action="store_true", help="machine readable")
    ap.add_argument("--save", action="store_true", help="also write nse/news.json for other skills")
    args = ap.parse_args(argv)

    try:
        rows = gather(args.symbol, args.days, args.tag)
    except NotPublished as err:
        print(f"The exchange gave nothing: {err}")
        return 2

    shown = rows[:args.top]
    if args.save:
        path = show.hand_off("news.json", rows)
        print(f"wrote {len(rows)} announcements to {path}\n")

    header = (f"{args.symbol.upper()} announcements" if args.symbol
              else "Market announcements") + f", last {args.days} day(s)"
    if args.tag:
        header += f", {args.tag} only"
    if not args.json:
        print(f"{header}. {len(rows)} filed, showing {len(shown)}.\n")

    show.emit(shown, args.json,
              [("when", "when", 16), ("symbol", "symbol", 12), ("tag", "tag", TAG_WIDTH),
               ("kind", "kind", 26), ("said", "what was filed", 62)],
              empty="Nothing was filed in that window.",
              note="An announcement is a fact about a filing, not about a price. "
                   "Check /nse-delivery for whether anyone acted on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
