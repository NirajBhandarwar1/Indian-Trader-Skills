"""Printing a table, and the one rule every skill here follows about empty results.

Nothing is padded out and nothing is invented. A skill that found three rows says three, and a skill
that found none says so plainly rather than printing an empty frame that looks like a failure.
"""
from __future__ import annotations

import json as _json
from pathlib import Path


def table(rows: list[dict], columns: list[tuple[str, str, int]], empty: str = "Nothing matched.") -> None:
    if not rows:
        print(empty)
        return
    print("  ".join(f"{title:<{w}}" for _k, title, w in columns))
    print("  ".join("-" * w for _k, _t, w in columns))
    for row in rows:
        cells = []
        for key, _title, width in columns:
            value = row.get(key)
            text = "" if value is None else str(value)
            if len(text) > width:
                text = text[:width - 1] + "…"
            cells.append(f"{text:<{width}}")
        print("  ".join(cells))


def emit(rows, as_json: bool, columns=None, empty="Nothing matched.", note: str = "") -> None:
    """Either the machine-readable form or the table, never both."""
    if as_json:
        print(_json.dumps(rows, indent=2, default=str))
        return
    table(rows, columns or [], empty)
    if note and rows:
        print(f"\n{note}")


def shared(name: str) -> Path:
    """Where skills leave artifacts for one another: an `nse/` folder beside the working directory.

    Named rather than guessed so two skills cannot disagree about it, and created on demand so a
    fresh checkout does not need a setup step.
    """
    folder = Path.cwd() / "nse"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / name


def hand_off(name: str, rows) -> Path:
    """Write what the next skill will read, and say where it went."""
    path = shared(name)
    path.write_text(_json.dumps(rows, indent=2, default=str), encoding="utf-8")
    return path
