"""Printing a table, and the one rule every skill here follows about empty results.

Nothing is padded out and nothing is invented. A skill that found three rows says three, and a skill
that found none says so plainly rather than printing an empty frame that looks like a failure.
"""
from __future__ import annotations

import argparse as _argparse
import json as _json
import math as _math
import os as _os
import sys as _sys
from pathlib import Path


def _text_out() -> None:
    """Make printing company text safe wherever the output goes.

    Filings and headlines carry the rupee sign, curly quotes and dashes. A Windows console prints
    them, but when the output goes to a pipe or a file Python falls back to the legacy code page,
    and one headline with "Rs" written as the rupee sign stopped the news skill with a crash
    halfway through its report. Redirected output is written as UTF-8 instead; anything a console
    still cannot show is replaced rather than allowed to stop the run.
    """
    for stream in (_sys.stdout, _sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_text_out()


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
                # Three dots rather than a single ellipsis character: a Windows console running
                # the default code page cannot encode U+2026 and prints a replacement glyph in
                # the middle of the table.
                text = text[:max(width - 3, 0)] + "..."
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


def hand_off(name: str, rows, out: str | Path | None = None) -> Path:
    """Write what the next skill will read, and say where it went.

    `out` is the --out flag: write somewhere else instead. The next skill still looks in nse/ by
    default, so a file written elsewhere has to be handed to it explicitly.
    """
    path = Path(out) if out else shared(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Whole or not at all, as the US pack writes it: into a file beside it, then moved over it,
    # so the next skill can never read half a file. A failure still raises, and leaves no litter.
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(_json.dumps(rows, indent=2, default=str), encoding="utf-8")
        _os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def number(text) -> float:
    """An argparse type: a finite number. Plain float() lets "nan" and "inf" through, and every
    comparison against nan is False, so a stop of nan passes "stop below entry" checks unseen."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise _argparse.ArgumentTypeError(f"wants a number, got {text!r}") from None
    if not _math.isfinite(value):
        raise _argparse.ArgumentTypeError(f"wants a number, got {text!r}")
    return value


def run(main) -> None:
    """Run a skill's main(), turning a bad clock or an unknown calendar into a plain message.

    The US pack answers a malformed --now with one line saying what it wants. Without this, six
    skills here printed a Python traceback for the same mistake.
    """
    import sys
    from . import clock, settings
    try:
        code = main()
    except (clock.BadTime, clock.Unknown, settings.BadConfig) as err:
        print(f"{Path(sys.argv[0]).name}: {err}", file=sys.stderr)
        code = 2
    raise SystemExit(code)

