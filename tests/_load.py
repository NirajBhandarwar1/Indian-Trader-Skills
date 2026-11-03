"""Loading a skill's script, and the saved responses the tests run against.

Every skill here is a standalone script in its own folder rather than an importable package, which
is what makes each one readable on its own. That costs one helper: a loader that imports a file by
path. It lives here so eleven test files do not each carry a copy.

The fixtures are REAL RESPONSES, saved. A test written against made-up data proves the code agrees
with the test author; a test written against what the exchange actually sent proves it agrees with
the exchange. Each file says where and when it came from.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Pin the ONE source of the shared code before any skill is loaded.
#
# Every skill folder carries its own copy of nsetools, and a skill puts its own folder first on the
# path when it runs. Inside one test process Python keeps whichever `nsetools` it imported first,
# so without this the package in use would depend on which test file happened to load first -- and
# a skill that needs only three modules could leave a three-module package behind for the next
# test. Pinning the source here makes every in-process test run against it. That the per-skill
# copies are identical to it is checked separately, byte for byte, and that each skill runs from
# its own folder alone is checked in a separate process by tests/test_standalone.py.
import sys as _sys

if str(ROOT) in _sys.path:
    _sys.path.remove(str(ROOT))
_sys.path.insert(0, str(ROOT))
import nsetools as _pinned                                       # noqa: E402,F401

assert Path(_pinned.__file__).resolve().parent == ROOT / "nsetools", \
    f"the wrong nsetools was loaded first: {_pinned.__file__}"


# NO NETWORK, ENFORCED RATHER THAN HOPED FOR.
#
# These tests are meant to run on saved real responses and nothing else. That was once checked by
# running the suite behind a dead proxy -- with no_proxy set to '*', which tells Python to skip the
# proxy for every host, so nothing was blocked at all and the check proved nothing. Replacing
# urlopen inside the test process cannot be bypassed that way: any code that reaches for the
# network gets a refusal, and a test that secretly depended on the network fails here.
import urllib.error as _urlerror                                  # noqa: E402
import urllib.request as _urlrequest                              # noqa: E402


def _no_network(*_args, **_kwargs):
    raise _urlerror.URLError("the network is blocked in the tests: use a saved response")


_urlrequest.urlopen = _no_network

# The exchange's equity master list says which symbols are companies. Several skills ask for it,
# so the saved copy stands in for the live one for the whole test process.
from nsetools import nse as _nse                                  # noqa: E402

if (FIXTURES / "exchange" / "equities.json.gz").exists():
    def _saved_equities():
        import gzip as _gz
        import json as _js
        body = _js.loads(_gz.decompress(
            (FIXTURES / "exchange" / "equities.json.gz").read_bytes()).decode("utf-8"))
        return body["data"] if isinstance(body, dict) and "data" in body else body

    _nse.equities = _saved_equities
    _nse._equity_symbols = None


def skill(folder: str, module: str):
    """Import a skill's script by path, as `skill("nse-entry", "entry")`."""
    spec = importlib.util.spec_from_file_location(module, ROOT / folder / f"{module}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def raw(name: str) -> bytes:
    """A saved response exactly as it arrived, bytes, un-gzipping the stored file if needed.

    Use this where the code under test does its own parsing, so the parser is what is being
    tested rather than this helper.
    """
    path = FIXTURES / name
    if not path.exists():
        raise FileNotFoundError(f"no fixture at {path}. Run: python tests/fixtures/capture.py")
    body = path.read_bytes()
    return gzip.decompress(body) if path.suffix == ".gz" else body


def fixture(name: str):
    """The `data` inside a saved response, with the provenance wrapper taken off.

    capture.py stores parsed responses as {"_source", "_captured", "_note", "data"} so every
    fixture can say where it came from. Tests want the data.
    """
    text = raw(name).decode("utf-8")
    body = json.loads(text)
    return body["data"] if isinstance(body, dict) and "data" in body else body


def provenance(name: str) -> dict:
    """Where a fixture came from and when, for the test that checks they are all real."""
    body = json.loads(raw(name).decode("utf-8"))
    return {k: body.get(k) for k in ("_source", "_captured", "_note")} \
        if isinstance(body, dict) else {}


def has(name: str) -> bool:
    return (FIXTURES / name).exists()


def path(name: str) -> Path:
    """The fixture's own path, for code that takes an --offline file."""
    return FIXTURES / name


def price_cache(into: Path):
    """A real price cache, rebuilt from the saved daily bars.

    1,875 real daily bars for 25 liquid symbols over 75 trading days, straight out of the
    exchange's own bhavcopy by way of nse/prices.sqlite. The screen and the replay both run on
    this, so what they are measured on is the market rather than a shape chosen to make them pass.
    """
    import sys
    from datetime import date as _date
    sys.path.insert(0, str(ROOT))
    from nsetools.store import Prices

    saved = fixture("cache/daily-bars.json.gz")
    store = Prices(into)
    by_day: dict[str, list[dict]] = {}
    for row in saved["bars"]:
        # put_day reads the bhavcopy's own column name, which is `turnover`; the stored column is
        # `turnover_lacs`. Feeding the stored name leaves turnover NULL, every row then fails the
        # liquidity floor, and every setup fires on nothing at all.
        by_day.setdefault(row["traded_on"], []).append(
            dict(row, turnover=row.get("turnover_lacs")))
    for when, rows in sorted(by_day.items()):
        store.put_day(_date.fromisoformat(when), rows)
    for summary in saved.get("measured") or []:
        store.remember({**summary,
                        "average_percent": summary.get("average"),
                        "median_percent": summary.get("median"),
                        "baseline_average_percent": summary.get("baseline"),
                        "edge_over_baseline": summary.get("edge")})
    return store, saved
