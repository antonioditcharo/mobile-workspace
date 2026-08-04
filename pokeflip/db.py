"""SQLite access layer.

Everything the app knows lives in one file so a flipper can back it up by
copying it. Connections are per-call and short-lived; WAL keeps the scheduler
and the web server out of each other's way.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def day(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).date().isoformat()


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            with conn:
                conn.executescript(SCHEMA_PATH.read_text())
        finally:
            conn.close()

    # --- generic helpers ------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        conn = self.connect()
        try:
            return conn.execute(sql, tuple(params)).fetchone()
        finally:
            conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.tx() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.lastrowid or cur.rowcount

    # --- catalog --------------------------------------------------------

    def upsert_set(self, data: dict[str, Any]) -> None:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO sets (id, name, series, printed_total, total, release_date,
                                  symbol_url, logo_url, updated_at)
                VALUES (:id, :name, :series, :printed_total, :total, :release_date,
                        :symbol_url, :logo_url, :updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, series=excluded.series,
                    printed_total=excluded.printed_total, total=excluded.total,
                    release_date=excluded.release_date, symbol_url=excluded.symbol_url,
                    logo_url=excluded.logo_url, updated_at=excluded.updated_at
                """,
                {"updated_at": iso(), **data},
            )

    def upsert_cards(self, cards: Iterable[dict[str, Any]]) -> int:
        rows = [{"updated_at": iso(), **c} for c in cards]
        if not rows:
            return 0
        with self.tx() as conn:
            conn.executemany(
                """
                INSERT INTO cards (id, name, set_id, set_name, number, rarity, supertype,
                                   subtypes, artist, image_small, image_large,
                                   tcgplayer_url, cardmarket_url, updated_at)
                VALUES (:id, :name, :set_id, :set_name, :number, :rarity, :supertype,
                        :subtypes, :artist, :image_small, :image_large,
                        :tcgplayer_url, :cardmarket_url, :updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, set_id=excluded.set_id, set_name=excluded.set_name,
                    number=excluded.number, rarity=excluded.rarity,
                    supertype=excluded.supertype, subtypes=excluded.subtypes,
                    artist=excluded.artist, image_small=excluded.image_small,
                    image_large=excluded.image_large,
                    tcgplayer_url=excluded.tcgplayer_url,
                    cardmarket_url=excluded.cardmarket_url,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get_card(self, card_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM cards WHERE id = ?", (card_id,))

    def search_cards(self, term: str, limit: int = 50) -> list[sqlite3.Row]:
        like = f"%{term.strip()}%"
        return self.query(
            """
            SELECT * FROM cards
            WHERE name LIKE ? OR id LIKE ? OR set_name LIKE ?
            ORDER BY name, set_name, CAST(number AS INTEGER)
            LIMIT ?
            """,
            (like, like, like, limit),
        )

    # --- prices ---------------------------------------------------------

    def record_prices(self, quotes: Iterable[dict[str, Any]]) -> int:
        """Insert price snapshots. One row per card/source/variant/day wins;
        a re-run on the same day refreshes that day's row rather than
        double-counting it in the trend math."""
        rows = list(quotes)
        if not rows:
            return 0
        with self.tx() as conn:
            conn.executemany(
                """
                INSERT INTO price_history
                    (card_id, source, variant, captured_at, captured_on, currency,
                     market, low, mid, high, direct_low)
                VALUES (:card_id, :source, :variant, :captured_at, :captured_on, :currency,
                        :market, :low, :mid, :high, :direct_low)
                ON CONFLICT(card_id, source, variant, captured_on) DO UPDATE SET
                    captured_at=excluded.captured_at, market=excluded.market,
                    low=excluded.low, mid=excluded.mid, high=excluded.high,
                    direct_low=excluded.direct_low, currency=excluded.currency
                """,
                rows,
            )
        return len(rows)

    def price_series(
        self,
        card_id: str,
        variant: str,
        source: str,
        days: int = 180,
    ) -> list[sqlite3.Row]:
        since = day(utcnow() - timedelta(days=days))
        return self.query(
            """
            SELECT * FROM price_history
            WHERE card_id = ? AND variant = ? AND source = ? AND captured_on >= ?
            ORDER BY captured_on
            """,
            (card_id, variant, source, since),
        )

    def latest_price(self, card_id: str, variant: str, source: str) -> sqlite3.Row | None:
        return self.one(
            """
            SELECT * FROM price_history
            WHERE card_id = ? AND variant = ? AND source = ?
            ORDER BY captured_on DESC LIMIT 1
            """,
            (card_id, variant, source),
        )

    def tracked_variants(self, source: str, card_ids: Iterable[str] | None = None
                         ) -> list[tuple[str, str]]:
        """Distinct (card_id, variant) pairs that have any price history."""
        sql = "SELECT DISTINCT card_id, variant FROM price_history WHERE source = ?"
        params: list[Any] = [source]
        ids = list(card_ids or [])
        if ids:
            sql += f" AND card_id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        return [(r["card_id"], r["variant"]) for r in self.query(sql, params)]

    def prune_history(self, retention_days: int) -> int:
        cutoff = day(utcnow() - timedelta(days=retention_days))
        with self.tx() as conn:
            cur = conn.execute("DELETE FROM price_history WHERE captured_on < ?", (cutoff,))
            return cur.rowcount

    # --- runs -----------------------------------------------------------

    def start_run(self, kind: str) -> int:
        return self.execute(
            "INSERT INTO runs (kind, status, started_at) VALUES (?, 'running', ?)",
            (kind, iso()),
        )

    def finish_run(self, run_id: int, status: str, stats: dict | None = None,
                   error: str | None = None) -> None:
        self.execute(
            "UPDATE runs SET status=?, finished_at=?, stats=?, error=? WHERE id=?",
            (status, iso(), json.dumps(stats or {}), error, run_id),
        )

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    # --- settings -------------------------------------------------------

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
