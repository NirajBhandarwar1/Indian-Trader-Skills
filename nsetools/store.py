"""A local cache of daily bars, so a backtest runs offline instead of re-asking the exchange.

One request gets one trading day for every stock, which is the right shape for this: 250 requests
buys a year of the whole market rather than a year of one symbol. Cached in SQLite so the second
run costs nothing and the exchange is asked once per day, ever.

Standard library only. sqlite3 ships with Python.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    traded_on        TEXT NOT NULL,     -- YYYY-MM-DD
    symbol           TEXT NOT NULL,
    open             REAL,
    high             REAL,
    low              REAL,
    close            REAL,
    previous_close   REAL,
    volume           REAL,
    turnover_lacs    REAL,
    delivered        REAL,
    delivery_percent REAL,
    PRIMARY KEY (traded_on, symbol)
);
CREATE INDEX IF NOT EXISTS bars_symbol ON bars (symbol, traded_on);
-- Which days have been fetched, including the ones the exchange had nothing for. Without this a
-- weekend is re-requested on every run, and a holiday looks like a day still to collect.
CREATE TABLE IF NOT EXISTS days (
    traded_on TEXT PRIMARY KEY,
    rows      INTEGER NOT NULL,
    note      TEXT
);
-- What a replay measured for a setup, so a screen can print the rule's record beside the stocks it
-- found. A screener that shows candidates without the rule's history invites you to act on a rule
-- nobody checked, which is the whole failure this pack is trying not to repeat.
CREATE TABLE IF NOT EXISTS measured (
    setup       TEXT NOT NULL,
    hold_days   INTEGER NOT NULL,
    stop_percent REAL NOT NULL,
    trades      INTEGER NOT NULL,
    win_rate    REAL,
    average     REAL,
    median      REAL,
    baseline    REAL,
    edge        REAL,
    window      TEXT,
    measured_on TEXT NOT NULL,
    PRIMARY KEY (setup, hold_days, stop_percent)
);
"""


class Prices:
    def __init__(self, path: str | Path = "nse/prices.sqlite") -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        try:
            self.db.executescript(SCHEMA)
            self.db.commit()
        except sqlite3.DatabaseError:
            # A file that is not a cache: let go of it before saying so, or Windows keeps it
            # locked for the life of the process.
            self.db.close()
            raise

    # ---- writing ------------------------------------------------------------------------------

    def put_day(self, day: date, rows: list[dict], note: str = "") -> int:
        """Store one day. Returns how many bars were new; re-running a day adds nothing."""
        before = self.db.total_changes
        self.db.executemany(
            "INSERT OR IGNORE INTO bars (traded_on, symbol, open, high, low, close, "
            "previous_close, volume, turnover_lacs, delivered, delivery_percent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(str(day), r.get("symbol"), r.get("open"), r.get("high"), r.get("low"),
              r.get("close"), r.get("previous_close"), r.get("volume"), r.get("turnover"),
              r.get("delivered"), r.get("delivery_percent")) for r in rows if r.get("symbol")])
        self.db.execute("INSERT OR REPLACE INTO days (traded_on, rows, note) VALUES (?, ?, ?)",
                        (str(day), len(rows), note or None))
        self.db.commit()
        return self.db.total_changes - before

    def fetched(self) -> set[str]:
        """Every day already asked about, whether or not it had data."""
        return {r[0] for r in self.db.execute("SELECT traded_on FROM days")}

    # ---- reading ------------------------------------------------------------------------------

    def trading_days(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT traded_on FROM days WHERE rows > 0 ORDER BY traded_on")]

    def day(self, traded_on: str) -> list[dict]:
        cur = self.db.execute(
            "SELECT symbol, open, high, low, close, previous_close, volume, turnover_lacs, "
            "delivery_percent FROM bars WHERE traded_on = ?", (traded_on,))
        keys = ("symbol", "open", "high", "low", "close", "previous_close", "volume",
                "turnover_lacs", "delivery_percent")
        return [dict(zip(keys, row)) for row in cur]

    def series(self, symbol: str) -> list[dict]:
        cur = self.db.execute(
            "SELECT traded_on, open, high, low, close, previous_close, volume, turnover_lacs, "
            "delivery_percent FROM bars WHERE symbol = ? ORDER BY traded_on", (symbol.upper(),))
        keys = ("traded_on", "open", "high", "low", "close", "previous_close", "volume",
                "turnover_lacs", "delivery_percent")
        return [dict(zip(keys, row)) for row in cur]

    def remember(self, summary: dict) -> None:
        """Keep what a replay found, so a screen can show the rule's record beside its candidates."""
        from datetime import datetime
        self.db.execute(
            "INSERT OR REPLACE INTO measured (setup, hold_days, stop_percent, trades, win_rate, "
            "average, median, baseline, edge, window, measured_on) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (summary["setup"], summary.get("hold_days", 0), summary.get("stop_percent", 0.0),
             summary.get("trades", 0), summary.get("win_rate"), summary.get("average_percent"),
             summary.get("median_percent"), summary.get("baseline_average_percent"),
             summary.get("edge_over_baseline"), summary.get("window"),
             datetime.now().strftime("%Y-%m-%d %H:%M")))
        self.db.commit()

    def record_for(self, setup: str) -> dict | None:
        """The most recently measured record for a setup, whatever the hold and stop."""
        row = self.db.execute(
            "SELECT setup, hold_days, stop_percent, trades, win_rate, average, median, baseline, "
            "edge, window, measured_on FROM measured WHERE setup = ? "
            "ORDER BY measured_on DESC LIMIT 1", (setup,)).fetchone()
        if not row:
            return None
        keys = ("setup", "hold_days", "stop_percent", "trades", "win_rate", "average", "median",
                "baseline", "edge", "window", "measured_on")
        return dict(zip(keys, row))

    def counts(self) -> dict:
        days = self.db.execute("SELECT COUNT(*) FROM days WHERE rows > 0").fetchone()[0]
        bars = self.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        syms = self.db.execute("SELECT COUNT(DISTINCT symbol) FROM bars").fetchone()[0]
        span = self.db.execute("SELECT MIN(traded_on), MAX(traded_on) FROM bars").fetchone()
        return {"days": days, "bars": bars, "symbols": syms,
                "first": span[0], "last": span[1],
                "megabytes": round(self.path.stat().st_size / 1e6, 1) if self.path.exists() else 0}

    def close(self) -> None:
        self.db.close()


def missing_days(store: Prices, back: int, until: date | None = None) -> list[date]:
    """Weekdays in the window that have not been asked about yet, newest first.

    Weekends are skipped outright and holidays are remembered once asked, so a second run over the
    same window costs no requests at all.
    """
    until = until or date.today()
    already = store.fetched()
    out, day, looked = [], until, 0
    while len(out) < back and looked < back * 2 + 20:
        looked += 1
        if day.weekday() < 5 and str(day) not in already:
            out.append(day)
        day -= timedelta(days=1)
    return out
