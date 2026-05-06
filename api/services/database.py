import asyncio
import logging
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS subnet_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    netuid INTEGER NOT NULL,
    alpha_price_tao REAL,
    tao_price_usd REAL,
    tao_in REAL,
    alpha_in REAL,
    total_stake REAL,
    emission_rate REAL,
    validator_count INTEGER,
    neuron_count INTEGER,
    UNIQUE(block, netuid)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_netuid_block
    ON subnet_snapshots(netuid, block);

CREATE INDEX IF NOT EXISTS idx_snapshots_netuid_ts
    ON subnet_snapshots(netuid, timestamp);

-- Standalone timestamp index lets queries across all subnets use the index
-- (the composite index above can't help when netuid isn't in the predicate).
CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp
    ON subnet_snapshots(timestamp);

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    netuid INTEGER,               -- NULL means any subnet
    metric TEXT NOT NULL,         -- alpha_price_tao | tao_in | alpha_in | market_cap_tao
    threshold REAL NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('above','below','cross_up','cross_down')),
    created_at TEXT NOT NULL,
    last_fired_at TEXT,
    last_value REAL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_webhooks_active ON webhook_subscriptions(active);

CREATE TABLE IF NOT EXISTS tracked_wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coldkey_ss58 TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT NOT NULL,
    last_polled_at TEXT,
    poll_interval_seconds INTEGER NOT NULL DEFAULT 300,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_tracked_wallets_active ON tracked_wallets(active);

CREATE TABLE IF NOT EXISTS wallet_portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coldkey_ss58 TEXT NOT NULL,
    block INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    total_balance_tao REAL,
    free_balance_tao REAL,
    total_staked_tao REAL,
    tao_price_usd REAL,
    total_balance_usd REAL,
    subnet_count INTEGER,
    UNIQUE(coldkey_ss58, block)
);

CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_coldkey_ts
    ON wallet_portfolio_snapshots(coldkey_ss58, timestamp);
"""


class Database:
    def __init__(self, db_path: str):
        self._path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        # Serialize writes so the poller + webhook evaluator don't interleave
        # transactions on the shared aiosqlite connection. Reads are
        # unguarded; SQLite handles concurrent readers fine.
        self._write_lock = asyncio.Lock()

    async def startup(self):
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info(f"Database ready at {self._path}")

    async def shutdown(self):
        if self._db:
            await self._db.close()

    async def insert_snapshot(self, data: dict) -> bool:
        """Insert a snapshot row. Returns True if inserted, False if a row
        for (block, netuid) already existed."""
        async with self._write_lock:
            try:
                cursor = await self._db.execute(
                    """INSERT OR IGNORE INTO subnet_snapshots
                       (block, timestamp, netuid, alpha_price_tao, tao_price_usd,
                        tao_in, alpha_in, total_stake, emission_rate,
                        validator_count, neuron_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        data["block"],
                        data["timestamp"],
                        data["netuid"],
                        data.get("alpha_price_tao"),
                        data.get("tao_price_usd"),
                        data.get("tao_in"),
                        data.get("alpha_in"),
                        data.get("total_stake"),
                        data.get("emission_rate"),
                        data.get("validator_count"),
                        data.get("neuron_count"),
                    ),
                )
                await self._db.commit()
                return cursor.rowcount > 0
            except aiosqlite.IntegrityError:
                # Duplicate (block, netuid): a poller race or resume-replay.
                return False
            except Exception:
                logger.exception("Failed to insert snapshot")
                return False

    async def insert_batch(self, rows: list[dict]) -> int:
        """Insert multiple snapshots. Returns count of new rows."""
        if not rows:
            return 0
        inserted = 0
        for row in rows:
            if await self.insert_snapshot(row):
                inserted += 1
        return inserted

    async def get_latest_block(self, netuid: int) -> Optional[int]:
        """Get the most recent block we have for a subnet."""
        cursor = await self._db.execute(
            "SELECT MAX(block) FROM subnet_snapshots WHERE netuid = ?",
            (netuid,),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    async def get_price_history(
        self, netuid: int, hours: int = 24, limit: int = 500
    ) -> list[dict]:
        """Get alpha price history for a subnet over the last N hours.

        Uses ``datetime(timestamp)`` so SQLite parses the ISO-with-offset
        string we store (``2026-04-20T18:45:00+00:00``) rather than
        comparing strings lexically.
        """
        cursor = await self._db.execute(
            """SELECT block, timestamp, alpha_price_tao, tao_price_usd
               FROM subnet_snapshots
               WHERE netuid = ?
                 AND datetime(timestamp) >= datetime('now', ?)
               ORDER BY block ASC
               LIMIT ?""",
            (netuid, f"-{hours} hours", limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_snapshots(
        self,
        netuid: int,
        hours: int = 24,
        limit: int = 500,
    ) -> list[dict]:
        """Get full snapshots for a subnet over the last N hours."""
        cursor = await self._db.execute(
            """SELECT block, timestamp, netuid, alpha_price_tao, tao_price_usd,
                      tao_in, alpha_in, total_stake, emission_rate,
                      validator_count, neuron_count
               FROM subnet_snapshots
               WHERE netuid = ?
                 AND datetime(timestamp) >= datetime('now', ?)
               ORDER BY block ASC
               LIMIT ?""",
            (netuid, f"-{hours} hours", limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_stats(self, netuid: int) -> dict:
        """Get summary stats for a subnet's historical data."""
        cursor = await self._db.execute(
            """SELECT
                 MIN(block) as earliest_block,
                 MAX(block) as latest_block,
                 MIN(timestamp) as earliest_time,
                 MAX(timestamp) as latest_time,
                 COUNT(*) as total_snapshots
               FROM subnet_snapshots
               WHERE netuid = ?""",
            (netuid,),
        )
        row = await cursor.fetchone()
        if not row or row["total_snapshots"] == 0:
            return {
                "netuid": netuid,
                "earliest_block": None,
                "latest_block": None,
                "earliest_time": None,
                "latest_time": None,
                "total_snapshots": 0,
            }
        return {
            "netuid": netuid,
            "earliest_block": row["earliest_block"],
            "latest_block": row["latest_block"],
            "earliest_time": row["earliest_time"],
            "latest_time": row["latest_time"],
            "total_snapshots": row["total_snapshots"],
        }

    async def get_candles(
        self,
        netuid: int,
        interval_seconds: int,
        hours: int,
    ) -> list[dict]:
        """Aggregate ``subnet_snapshots`` into OHLC buckets.

        For each bucket we compute open/close by block order (so a single
        bucket with one sample still returns a sensible candle) and
        high/low as MIN/MAX across ``alpha_price_tao``.
        """
        bucket = int(interval_seconds)
        window = f"-{int(hours)} hours"
        cursor = await self._db.execute(
            """
            WITH ranked AS (
                SELECT
                    (CAST(strftime('%s', timestamp) AS INTEGER) / :bucket) * :bucket AS t,
                    alpha_price_tao AS price,
                    block,
                    ROW_NUMBER() OVER (
                        PARTITION BY (CAST(strftime('%s', timestamp) AS INTEGER) / :bucket)
                        ORDER BY block ASC
                    ) AS rn_asc,
                    ROW_NUMBER() OVER (
                        PARTITION BY (CAST(strftime('%s', timestamp) AS INTEGER) / :bucket)
                        ORDER BY block DESC
                    ) AS rn_desc
                FROM subnet_snapshots
                WHERE netuid = :netuid
                  AND datetime(timestamp) >= datetime('now', :window)
                  AND alpha_price_tao IS NOT NULL
            )
            SELECT
                t,
                MAX(CASE WHEN rn_asc  = 1 THEN price END) AS open,
                MAX(CASE WHEN rn_desc = 1 THEN price END) AS close,
                MAX(price) AS high,
                MIN(price) AS low,
                COUNT(*) AS samples
            FROM ranked
            GROUP BY t
            ORDER BY t ASC
            """,
            {
                "bucket": bucket,
                "netuid": netuid,
                "window": window,
            },
        )
        rows = await cursor.fetchall()
        return [
            {
                "t": int(r["t"]),
                "o": r["open"],
                "h": r["high"],
                "l": r["low"],
                "c": r["close"],
                "n": r["samples"],
            }
            for r in rows
        ]

    async def get_snapshot_count(self) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM subnet_snapshots"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    # --- Webhooks ---

    async def create_webhook(
        self,
        url: str,
        metric: str,
        threshold: float,
        direction: str,
        netuid: int | None,
        created_at: str,
    ) -> int:
        async with self._write_lock:
            cursor = await self._db.execute(
                """INSERT INTO webhook_subscriptions
                     (url, netuid, metric, threshold, direction, created_at, active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (url, netuid, metric, threshold, direction, created_at),
            )
            await self._db.commit()
            return cursor.lastrowid

    async def get_webhook(self, sub_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM webhook_subscriptions WHERE id = ?", (sub_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_active_webhooks(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM webhook_subscriptions WHERE active = 1"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def deactivate_webhook(self, sub_id: int) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE webhook_subscriptions SET active = 0 WHERE id = ?",
                (sub_id,),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def update_webhook_fired(
        self, sub_id: int, value: float, fired_at: str
    ) -> None:
        async with self._write_lock:
            await self._db.execute(
                """UPDATE webhook_subscriptions
                   SET last_value = ?, last_fired_at = ?
                   WHERE id = ?""",
                (value, fired_at, sub_id),
            )
            await self._db.commit()

    async def update_webhook_value(self, sub_id: int, value: float) -> None:
        """Record observed value without marking a fire, used when we
        evaluate but don't cross the threshold."""
        async with self._write_lock:
            await self._db.execute(
                "UPDATE webhook_subscriptions SET last_value = ? WHERE id = ?",
                (value, sub_id),
            )
            await self._db.commit()

    async def get_latest_metric(self, netuid: int, metric: str) -> float | None:
        """Most recent value of a metric for a subnet. ``market_cap_tao`` is
        derived from tao_in (the pool-side TAO). Returns None if no data."""
        column_map = {
            "alpha_price_tao": "alpha_price_tao",
            "tao_in": "tao_in",
            "alpha_in": "alpha_in",
            "market_cap_tao": "tao_in",
        }
        col = column_map.get(metric)
        if col is None:
            return None
        cursor = await self._db.execute(
            f"""SELECT {col} FROM subnet_snapshots
                WHERE netuid = ?
                ORDER BY block DESC LIMIT 1""",
            (netuid,),
        )
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])

    async def get_missing_price_range(self) -> tuple[str, str] | None:
        """Min/max timestamp of rows with no USD price. Returns None if
        every row already has a price."""
        cursor = await self._db.execute(
            """SELECT MIN(timestamp), MAX(timestamp)
               FROM subnet_snapshots
               WHERE tao_price_usd IS NULL OR tao_price_usd = 0"""
        )
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return None
        return row[0], row[1]

    # --- Tracked wallets ---

    async def add_tracked_wallet(
        self,
        coldkey: str,
        label: str | None,
        poll_interval_seconds: int,
        created_at: str,
    ) -> dict:
        """Add a wallet to the watchlist. If it already exists (active or
        archived) flip ``active=1`` and update label/interval rather than
        inserting a duplicate. Returns the row."""
        async with self._write_lock:
            await self._db.execute(
                """INSERT INTO tracked_wallets
                       (coldkey_ss58, label, created_at, poll_interval_seconds, active)
                   VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT(coldkey_ss58) DO UPDATE SET
                       label = excluded.label,
                       poll_interval_seconds = excluded.poll_interval_seconds,
                       active = 1""",
                (coldkey, label, created_at, poll_interval_seconds),
            )
            await self._db.commit()
        cursor = await self._db.execute(
            "SELECT * FROM tracked_wallets WHERE coldkey_ss58 = ?", (coldkey,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}

    async def list_tracked_wallets(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM tracked_wallets"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY id ASC"
        cursor = await self._db.execute(sql)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_tracked_wallet(self, coldkey: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM tracked_wallets WHERE coldkey_ss58 = ?", (coldkey,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def deactivate_tracked_wallet(self, coldkey: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE tracked_wallets SET active = 0 WHERE coldkey_ss58 = ?",
                (coldkey,),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def get_wallets_due_for_poll(self, now_iso: str) -> list[dict]:
        """Active wallets where ``last_polled_at`` is null or older than
        their per-row interval. Compared in SQL via ``datetime()`` so the
        ISO-with-offset strings are parsed as timestamps."""
        cursor = await self._db.execute(
            """SELECT * FROM tracked_wallets
               WHERE active = 1
                 AND (last_polled_at IS NULL
                      OR (julianday(?) - julianday(last_polled_at)) * 86400.0
                          >= poll_interval_seconds)
               ORDER BY id ASC""",
            (now_iso,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def mark_wallet_polled(self, coldkey: str, polled_at: str) -> None:
        async with self._write_lock:
            await self._db.execute(
                "UPDATE tracked_wallets SET last_polled_at = ? WHERE coldkey_ss58 = ?",
                (polled_at, coldkey),
            )
            await self._db.commit()

    async def insert_wallet_snapshot(self, data: dict) -> bool:
        """Insert one wallet portfolio snapshot. Idempotent on (coldkey, block)."""
        async with self._write_lock:
            try:
                cursor = await self._db.execute(
                    """INSERT OR IGNORE INTO wallet_portfolio_snapshots
                         (coldkey_ss58, block, timestamp,
                          total_balance_tao, free_balance_tao, total_staked_tao,
                          tao_price_usd, total_balance_usd, subnet_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        data["coldkey_ss58"],
                        data["block"],
                        data["timestamp"],
                        data.get("total_balance_tao"),
                        data.get("free_balance_tao"),
                        data.get("total_staked_tao"),
                        data.get("tao_price_usd"),
                        data.get("total_balance_usd"),
                        data.get("subnet_count"),
                    ),
                )
                await self._db.commit()
                return cursor.rowcount > 0
            except Exception:
                logger.exception("Failed to insert wallet snapshot")
                return False

    async def get_wallet_history(
        self, coldkey: str, hours: int = 168, limit: int = 500
    ) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT block, timestamp, total_balance_tao, free_balance_tao,
                      total_staked_tao, tao_price_usd, total_balance_usd,
                      subnet_count
               FROM wallet_portfolio_snapshots
               WHERE coldkey_ss58 = ?
                 AND datetime(timestamp) >= datetime('now', ?)
               ORDER BY block ASC
               LIMIT ?""",
            (coldkey, f"-{hours} hours", limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_wallet_latest(self, coldkey: str) -> dict | None:
        cursor = await self._db.execute(
            """SELECT block, timestamp, total_balance_tao, free_balance_tao,
                      total_staked_tao, tao_price_usd, total_balance_usd,
                      subnet_count
               FROM wallet_portfolio_snapshots
               WHERE coldkey_ss58 = ?
               ORDER BY block DESC LIMIT 1""",
            (coldkey,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_tao_prices_hourly(self, prices_by_hour: dict[str, float]) -> int:
        """Fill ``tao_price_usd`` for rows whose timestamp falls in one of the
        supplied hour buckets (format ``YYYY-MM-DDTHH``). Only touches rows
        where the price is currently zero/NULL so re-running is idempotent.
        Returns the number of rows updated."""
        if not prices_by_hour:
            return 0
        updated = 0
        async with self._write_lock:
            for hour_key, price in prices_by_hour.items():
                cursor = await self._db.execute(
                    """UPDATE subnet_snapshots
                       SET tao_price_usd = ?
                       WHERE (tao_price_usd IS NULL OR tao_price_usd = 0)
                         AND substr(timestamp, 1, 13) = ?""",
                    (price, hour_key),
                )
                updated += cursor.rowcount
            await self._db.commit()
        return updated
