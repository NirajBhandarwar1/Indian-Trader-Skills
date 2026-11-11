"""Bonus issues and splits from the exchange's own record, with exact ratios.

No key, no account. This exists to answer one question about a per-share figure: has the share
basis changed since that year ended? A 1:1 bonus halves every historical per-share figure, so an
earnings-per-share series that does not know about it shows a 50% collapse where nothing happened.

THE RULE HERE IS FAIL-SAFE. An event this module cannot read exactly makes the window UNCERTAIN,
and an uncertain window means the caller refuses to publish a per-share figure rather than
publishing one that might be on the wrong basis. Skipping an event it does not recognise would be
the opposite: silent, and wrong in the direction of a plausible number.

Standard library only.
"""
from __future__ import annotations

import re
from datetime import date, datetime

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

# "Bonus 1:1", "Bonus- 1:2", "Bonus 2:5". a new shares for every b held, so the count becomes
# (a+b)/b times what it was. Measured against the exchange's own wording: of 773 share-affecting
# actions market-wide since 2022, the bonus lines are "Bonus a:b" in every case bar one hyphen.
BONUS = re.compile(r"\bbonus\b[\s\-]*(\d+)\s*:\s*(\d+)", re.I)

# "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share". Note "Rs10/-"
# with no space appears too, and both "Rs" and "Re" are used, so the currency word is optional.
SPLIT = re.compile(r"from\s*(?:rs|re)?\.?\s*([\d.]+)\s*/?-?\s*per\s*share\s*to\s*"
                   r"(?:rs|re)?\.?\s*([\d.]+)", re.I)

# Events that restate the per-share basis by an amount NO free source quantifies exactly. The test
# applied here is the accounting one: does the event force earlier per-share figures to be
# restated? A bonus and a split do, because they hand out shares without bringing anything in, and
# both are exact. A rights issue does it only for its bonus element, which depends on the price and
# the take-up. A capital reduction, an amalgamation or a scheme of arrangement each rewrite the
# count on their own terms. So these make the window uncertain.
#
# A BUYBACK IS DELIBERATELY ABSENT. It changes the count but restates nothing, so refusing a year
# because of a later buyback would throw away a good figure -- it did exactly that to TCS FY2023
# before this was corrected. What a buyback does do is make the year-end count differ from the
# weighted average the company divided by, and that is caught by the separate identity check,
# which refuses TCS FY2024 at 0.8% for precisely this reason. Two checks, two different jobs.
UNQUANTIFIABLE = re.compile(r"\brights?\b|capital\s+reduction|amalgamat|consolidat|"
                            r"scheme\s+of\s+arrangement|reverse\s+split", re.I)

# A subject this module did not classify but which carries a ratio is suspicious by construction:
# ratios appear in corporate actions precisely when a holding is being restated. Treated as
# uncertain rather than ignored, because the alternative is to miss an event in silence.
RATIO = re.compile(r"\b\d+\s*:\s*\d+\b")

# A bonus of preference shares leaves the equity count alone, but reading one as an equity bonus
# would multiply every per-share figure by its ratio. Rare -- three market-wide in four years --
# and refused rather than interpreted.
NOT_EQUITY = re.compile(r"ncrps|ncd\b|preference|debenture|warrant", re.I)


class Uncertain(Exception):
    """The basis cannot be established exactly. The caller must refuse, not estimate."""


def parse_day(raw: str) -> date | None:
    """'28-Oct-2024' to a date. Anything else becomes nothing rather than a guess."""
    parts = str(raw or "").strip().split("-")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[2]), MONTHS[parts[1][:3].upper()], int(parts[0]))
    except (KeyError, ValueError):
        return None


def factor(subject: str) -> float | None:
    """The exact multiplier this one action applies to the share count.

    Returns None for an action that leaves the count alone, such as a dividend or a demerger: a
    demerger hands out shares in the company being split off and does not touch the parent's own
    count. Raises Uncertain for anything that moves the count by an amount no free source pins
    down.
    """
    text = str(subject or "").strip()
    if not text:
        return None

    if UNQUANTIFIABLE.search(text):
        raise Uncertain(f"{text!r} changes the share count by terms this cannot read exactly")

    bonus = BONUS.search(text)
    if bonus:
        if NOT_EQUITY.search(text):
            raise Uncertain(f"{text!r} is a bonus of something other than equity shares")
        new, held = int(bonus.group(1)), int(bonus.group(2))
        if held <= 0:
            raise Uncertain(f"{text!r} has an unreadable bonus ratio")
        return (new + held) / held

    if re.search(r"face\s*value\s*split|sub-?division", text, re.I):
        split = SPLIT.search(text)
        if not split:
            raise Uncertain(f"{text!r} is a split whose face values could not be read")
        before, after = float(split.group(1)), float(split.group(2))
        if before <= 0 or after <= 0 or before <= after:
            raise Uncertain(f"{text!r} has face values that do not describe a split")
        return before / after

    if RATIO.search(text):
        raise Uncertain(f"{text!r} carries a ratio this module does not recognise")
    return None


def basis_change(rows: list[dict], since: date,
                 until: date | None = None) -> tuple[float, list[str]]:
    """The cumulative multiplier applied to the share basis after `since`, and what caused it.

    `until`, when given, closes the window: only events ex on or before it count, which is what
    comparing two periods needs. `rows` are the exchange's corporate actions. Raises Uncertain if
    any action in the window cannot be read exactly, because a factor that is missing one event is
    worse than no factor: it looks authoritative.

    FAIL CLOSED on a date that cannot be read. A bonus or split (or anything that might move the
    count) whose ex-date is unreadable could fall inside the window, so it raises Uncertain rather
    than being skipped. An unreadable date on an action that leaves the count alone (a dividend)
    changes nothing either way and is passed over. A row that is not a record at all raises too.
    """
    multiplier, why = 1.0, []
    for row in rows:
        if not isinstance(row, dict):
            raise Uncertain(f"the record of corporate actions has an entry that cannot be read "
                            f"({row!r})")
        when = parse_day(row.get("exDate") or "")
        if when is None:
            subject = str(row.get("subject") or "").strip()
            if factor(subject) is not None:                 # an Uncertain one raises here too
                raise Uncertain(f"{subject!r} has an ex-date that cannot be read "
                                f"({row.get('exDate')!r}), so whether it falls in the window is "
                                f"unknown")
            continue
        if when <= since or (until is not None and when > until):
            continue
        one = factor(row.get("subject") or "")              # may raise Uncertain
        if one is not None:
            multiplier *= one
            why.append(f"{str(row.get('subject') or '').strip()} ex {when:%Y-%m-%d} ({one:g}x)")
    return multiplier, why


def changed_since(rows: list[dict], year_end: str) -> tuple[float, list[str]]:
    """As basis_change, taking a '2023-03-31' style period end."""
    try:
        when = datetime.strptime(year_end, "%Y-%m-%d").date()
    except ValueError as err:
        raise Uncertain(f"{year_end!r} is not a readable period end") from err
    return basis_change(rows, when)
