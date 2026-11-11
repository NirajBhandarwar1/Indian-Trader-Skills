"""Your rules, read once, from the file you name or the usual place.

Every skill that has limits reads them through here, so `--config` means the same thing everywhere
and two skills cannot disagree about where the rules live.

Where it looks, in order:

  1. the file you pass with --config
  2. config.json in the folder you are working in
  3. nse/config.json, beside the files the skills hand to one another

It does NOT look beside the script. A skill installed on its own lives in a folder of skills, and a
config.json there would be shared by accident with every other skill in it.

Standard library only.
"""
from __future__ import annotations

import json
from pathlib import Path


class BadConfig(ValueError):
    """A config file that exists but cannot be used."""


def where(path: str | Path | None = None) -> Path | None:
    """The config file that will be read, or None if there is none."""
    if path:
        return Path(path)
    for candidate in (Path.cwd() / "config.json", Path.cwd() / "nse" / "config.json"):
        if candidate.exists():
            return candidate
    return None


def load(path: str | Path | None = None, defaults: dict | None = None) -> dict:
    """The rules, with defaults filled in for anything the file leaves out.

    Says which file it used under `_from` so the caller can print it. A file named explicitly with
    --config that does not exist is an error, because that is a typo and not a missing default.
    A config that exists but cannot be read is refused (BadConfig), as the US pack refuses it:
    sizing against the defaults while the user believes their own limits apply is false data.
    """
    out = dict(defaults or {})
    found = where(path)
    if found is None:
        return out
    if path and not found.exists():
        raise FileNotFoundError(f"no config at {found}")
    try:
        doc = json.loads(found.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as err:
        raise BadConfig(f"{found} is not valid JSON ({err}); fix it, or remove it to use the "
                        f"defaults") from None
    if not isinstance(doc, dict):
        raise BadConfig(f"{found} must be a JSON object of settings, not a {type(doc).__name__}")
    out.update(doc)
    out["_from"] = str(found)
    return out
