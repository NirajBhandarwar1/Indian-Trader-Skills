"""Talking to the exchange the way a browser does, with nothing installed.

The exchange turns away a client that has not visited the site first: the API endpoints need a
cookie from the home page and a Referer naming the page a browser would have been on. Get either
wrong and you get a 401 or an empty body rather than an error that says what is missing.

No third-party packages. That is deliberate -- a skill pack people install in one line should not
also ask them to pip install a TLS-impersonation library -- and it works: a cookie jar plus honest
browser headers gets 200 from the announcements, shareholding, bhavcopy and master-list endpoints.

One session per thread, because a session holds cookies and sharing one across threads interleaves
the warm-up.
"""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import threading
import time
import urllib.error
import urllib.request

HOME = "https://www.nseindia.com"
ARCHIVES = "https://nsearchives.nseindia.com"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/122.0.0.0 Safari/537.36")

# Asked of the exchange no faster than this, across every thread. It is not a published limit; it is
# politeness plus the fact that a blocked client looks exactly like a working one -- a refused
# request returns faster than a real one, so throughput RISES when you are cut off. If a run speeds
# up, suspect a block before celebrating.
REQUESTS_PER_SECOND = 2.0

_local = threading.local()
_pace = threading.Lock()
_free_at = 0.0


class NotPublished(Exception):
    """The exchange has nothing here. For a company that never filed, that is the right answer."""


def _wait_turn() -> None:
    global _free_at
    gap = 1.0 / REQUESTS_PER_SECOND
    with _pace:
        now = time.monotonic()
        wait = max(0.0, _free_at - now)
        _free_at = max(now, _free_at) + gap
    if wait:
        time.sleep(wait)


def _session():
    session = getattr(_local, "session", None)
    if session is None:
        jar = http.cookiejar.CookieJar()
        session = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        _local.session = session
        _local.visited = set()
        _wait_turn()
        try:
            session.open(urllib.request.Request(HOME + "/", headers=_headers(HOME)), timeout=30).read()
        except Exception:                      # noqa: BLE001  a failed warm-up is not fatal
            pass
    return session


def _headers(referer: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Connection": "keep-alive",
    }


def fetch(url: str, referer: str = HOME, timeout: float = 30) -> bytes:
    """One request, warming the session and visiting the referring page first if need be."""
    session = _session()
    if referer != HOME and referer not in _local.visited:
        _wait_turn()
        try:
            session.open(urllib.request.Request(referer, headers=_headers(HOME)),
                         timeout=timeout).read()
            _local.visited.add(referer)
        except Exception:                      # noqa: BLE001
            pass

    _wait_turn()
    try:
        response = session.open(urllib.request.Request(url, headers=_headers(referer)),
                                timeout=timeout)
    except urllib.error.HTTPError as err:
        raise NotPublished(f"the exchange answered {err.code} for {url}") from err
    except urllib.error.URLError as err:
        raise NotPublished(f"could not reach the exchange: {err.reason}") from err

    body = response.read()
    if response.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return body


def fetch_json(url: str, referer: str = HOME, timeout: float = 30):
    """One request, parsed. An unreadable body is reported as nothing published rather than crashing.

    The exchange answers a blocked request with an HTML holding page and status 200, which parses as
    neither JSON nor an error, so this is where that shows up.
    """
    body = fetch(url, referer, timeout)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        head = body[:80].decode("utf-8", "replace").replace("\n", " ")
        raise NotPublished(f"the answer was not readable ({head!r})") from err
