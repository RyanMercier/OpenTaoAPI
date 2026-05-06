import asyncio
import json
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

-- Paper trading -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    initial_capital_tao REAL NOT NULL,
    config_json TEXT NOT NULL,                -- TradingConfig serialized
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    -- Runtime state, updated each cycle so a restart can rehydrate.
    free_tao REAL,
    peak_value REAL,
    hotkey_cooldowns_json TEXT,
    last_cycle_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    netuid INTEGER NOT NULL,
    entry_block INTEGER NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    alpha_amount REAL NOT NULL,
    tao_invested REAL NOT NULL,
    strategy TEXT NOT NULL,
    hotkey_id INTEGER NOT NULL,
    FOREIGN KEY (portfolio_id) REFERENCES paper_portfolios(id)
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_portfolio
    ON paper_positions(portfolio_id);

CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,                      -- UUID from Trade.id
    portfolio_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    block INTEGER NOT NULL,
    netuid INTEGER NOT NULL,
    direction TEXT NOT NULL,                  -- 'buy' or 'sell'
    strategy TEXT NOT NULL,
    tao_amount REAL NOT NULL,
    alpha_amount REAL NOT NULL,
    spot_price REAL NOT NULL,
    effective_price REAL NOT NULL,
    slippage_pct REAL NOT NULL,
    signal_strength REAL,
    hotkey_id INTEGER,
    entry_price REAL,
    pnl_tao REAL,
    pnl_pct REAL,
    hold_duration_hours REAL,
    entry_strategy TEXT,
    FOREIGN KEY (portfolio_id) REFERENCES paper_portfolios(id)
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_portfolio_ts
    ON paper_trades(portfolio_id, timestamp);

CREATE TABLE IF NOT EXISTS paper_value_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    free_tao REAL NOT NULL,
    total_value_tao REAL NOT NULL,
    total_pnl_tao REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    num_open_positions INTEGER NOT NULL,
    FOREIGN KEY (portfolio_id) REFERENCES paper_portfolios(id)
);
CREATE INDEX IF NOT EXISTS idx_paper_value_history_portfolio_ts
    ON paper_value_history(portfolio_id, timestamp);
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

    # --- Paper trading ---

    async def create_paper_portfolio(
        self,
        name: str,
        initial_capital_tao: float,
        config_json: str,
        created_at: str,
    ) -> int:
        async with self._write_lock:
            cursor = await self._db.execute(
                """INSERT INTO paper_portfolios
                       (name, initial_capital_tao, config_json, created_at,
                        active, free_tao, peak_value, hotkey_cooldowns_json,
                        last_cycle_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?, NULL, NULL)""",
                (name, initial_capital_tao, config_json, created_at,
                 initial_capital_tao, initial_capital_tao),
            )
            await self._db.commit()
            return cursor.lastrowid

    async def list_paper_portfolios(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM paper_portfolios"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY id ASC"
        cursor = await self._db.execute(sql)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_paper_portfolio(self, portfolio_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM paper_portfolios WHERE id = ?", (portfolio_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_paper_portfolio_runtime(self, portfolio_id: int) -> dict | None:
        cursor = await self._db.execute(
            """SELECT free_tao, peak_value, hotkey_cooldowns_json, last_cycle_at
               FROM paper_portfolios WHERE id = ?""",
            (portfolio_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_paper_portfolio_runtime(
        self,
        portfolio_id: int,
        peak_value: float,
        free_tao: float,
        hotkey_cooldowns: dict,
        last_cycle_at: str,
    ) -> None:
        cooldowns_json = json.dumps({str(k): int(v) for k, v in hotkey_cooldowns.items()})
        async with self._write_lock:
            await self._db.execute(
                """UPDATE paper_portfolios
                   SET free_tao = ?, peak_value = ?,
                       hotkey_cooldowns_json = ?, last_cycle_at = ?
                   WHERE id = ?""",
                (free_tao, peak_value, cooldowns_json, last_cycle_at, portfolio_id),
            )
            await self._db.commit()

    async def set_paper_portfolio_active(
        self, portfolio_id: int, active: bool
    ) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE paper_portfolios SET active = ? WHERE id = ?",
                (1 if active else 0, portfolio_id),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def list_paper_positions(self, portfolio_id: int) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT netuid, entry_block, entry_time, entry_price,
                      alpha_amount, tao_invested, strategy, hotkey_id
               FROM paper_positions WHERE portfolio_id = ?
               ORDER BY netuid ASC""",
            (portfolio_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def replace_paper_positions(
        self, portfolio_id: int, positions: dict
    ) -> None:
        """Atomic replace: clear existing rows for this portfolio, then
        insert one per current Position. The trader holds the source of
        truth in memory; the table is just a crash-safe mirror."""
        async with self._write_lock:
            await self._db.execute(
                "DELETE FROM paper_positions WHERE portfolio_id = ?",
                (portfolio_id,),
            )
            for netuid, pos in positions.items():
                entry_time = (
                    pos.entry_time.isoformat()
                    if hasattr(pos.entry_time, "isoformat")
                    else str(pos.entry_time)
                )
                strategy = (
                    pos.strategy.value
                    if hasattr(pos.strategy, "value")
                    else str(pos.strategy)
                )
                await self._db.execute(
                    """INSERT INTO paper_positions
                         (portfolio_id, netuid, entry_block, entry_time,
                          entry_price, alpha_amount, tao_invested, strategy,
                          hotkey_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        portfolio_id, int(netuid), int(pos.entry_block),
                        entry_time, float(pos.entry_price),
                        float(pos.alpha_amount), float(pos.tao_invested),
                        strategy, int(pos.hotkey_id),
                    ),
                )
            await self._db.commit()

    async def insert_paper_trade(self, portfolio_id: int, trade) -> None:
        """Append one trade row. ``trade`` is an ``api.trading.models.Trade``
        but kept untyped here to avoid importing the trading package from
        the database service."""
        ts = (
            trade.timestamp.isoformat()
            if hasattr(trade.timestamp, "isoformat")
            else str(trade.timestamp)
        )
        async with self._write_lock:
            await self._db.execute(
                """INSERT INTO paper_trades
                     (id, portfolio_id, timestamp, block, netuid, direction,
                      strategy, tao_amount, alpha_amount, spot_price,
                      effective_price, slippage_pct, signal_strength,
                      hotkey_id, entry_price, pnl_tao, pnl_pct,
                      hold_duration_hours, entry_strategy)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.id, portfolio_id, ts, int(trade.block),
                    int(trade.netuid),
                    trade.direction.value if hasattr(trade.direction, "value") else str(trade.direction),
                    trade.strategy.value if hasattr(trade.strategy, "value") else str(trade.strategy),
                    float(trade.tao_amount), float(trade.alpha_amount),
                    float(trade.spot_price), float(trade.effective_price),
                    float(trade.slippage_pct),
                    float(trade.signal_strength) if trade.signal_strength is not None else None,
                    int(trade.hotkey_id) if trade.hotkey_id is not None else None,
                    float(trade.entry_price) if trade.entry_price is not None else None,
                    float(trade.pnl_tao) if trade.pnl_tao is not None else None,
                    float(trade.pnl_pct) if trade.pnl_pct is not None else None,
                    float(trade.hold_duration_hours) if trade.hold_duration_hours is not None else None,
                    trade.entry_strategy.value if (trade.entry_strategy and hasattr(trade.entry_strategy, "value")) else None,
                ),
            )
            await self._db.commit()

    async def list_paper_trades(
        self, portfolio_id: int, limit: int = 200
    ) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT id, timestamp, block, netuid, direction, strategy,
                      tao_amount, alpha_amount, spot_price, effective_price,
                      slippage_pct, signal_strength, hotkey_id,
                      entry_price, pnl_tao, pnl_pct, hold_duration_hours,
                      entry_strategy
               FROM paper_trades WHERE portfolio_id = ?
               ORDER BY datetime(timestamp) DESC LIMIT ?""",
            (portfolio_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_paper_trades(self, portfolio_id: int) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE portfolio_id = ?",
            (portfolio_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def insert_paper_value_history(
        self,
        portfolio_id: int,
        timestamp: str,
        free_tao: float,
        total_value_tao: float,
        total_pnl_tao: float,
        drawdown_pct: float,
        num_open_positions: int,
    ) -> None:
        async with self._write_lock:
            await self._db.execute(
                """INSERT INTO paper_value_history
                     (portfolio_id, timestamp, free_tao, total_value_tao,
                      total_pnl_tao, drawdown_pct, num_open_positions)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (portfolio_id, timestamp, float(free_tao),
                 float(total_value_tao), float(total_pnl_tao),
                 float(drawdown_pct), int(num_open_positions)),
            )
            await self._db.commit()

    async def get_paper_value_history(
        self, portfolio_id: int, hours: int = 168, limit: int = 5000
    ) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT timestamp, free_tao, total_value_tao, total_pnl_tao,
                      drawdown_pct, num_open_positions
               FROM paper_value_history
               WHERE portfolio_id = ?
                 AND datetime(timestamp) >= datetime('now', ?)
               ORDER BY datetime(timestamp) ASC
               LIMIT ?""",
            (portfolio_id, f"-{hours} hours", limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_paper_anchor_timestamp(self, portfolio_id: int) -> str | None:
        """Earliest value_history timestamp, used as the buy-and-hold
        anchor. Stable across window changes so the benchmark line
        doesn't shift when the user toggles 24h vs 7d vs 30d."""
        cursor = await self._db.execute(
            """SELECT timestamp FROM paper_value_history
               WHERE portfolio_id = ?
               ORDER BY datetime(timestamp) ASC LIMIT 1""",
            (portfolio_id,),
        )
        row = await cursor.fetchone()
        return row["timestamp"] if row else None

    async def compute_paper_benchmark_series(
        self,
        portfolio_id: int,
        timestamps: list[str],
        initial_capital_tao: float,
        exclude_netuids: list[int] | None = None,
        min_pool_depth_tao: float = 50.0,
    ) -> tuple[list[float], list[int]]:
        """Pool-weighted buy-and-hold benchmark series.

        The anchor is the portfolio's earliest value_history row. At the
        anchor we build a basket whose weight[n] = tao_in[n] / total, then
        convert to alpha holdings. At each requested timestamp we sum
        ``holdings[n] * alpha_price[n]_t`` using the latest subnet snapshot
        at or before that timestamp.

        Returns ``(values, universe_netuids)``. ``values`` aligns 1:1 with
        ``timestamps``; ``universe_netuids`` lists the netuids in the
        basket so the UI can label what's being benchmarked against.
        """
        if not timestamps:
            return [], []

        anchor_ts = await self.get_paper_anchor_timestamp(portfolio_id)
        if not anchor_ts:
            return [initial_capital_tao for _ in timestamps], []

        exclude = set(exclude_netuids or [])

        # Anchor universe: latest snapshot per netuid at-or-before the anchor.
        cursor = await self._db.execute(
            """SELECT s.netuid, s.alpha_price_tao, s.tao_in
               FROM subnet_snapshots s
               INNER JOIN (
                 SELECT netuid, MAX(block) AS max_block
                 FROM subnet_snapshots
                 WHERE datetime(timestamp) <= datetime(?)
                 GROUP BY netuid
               ) latest ON s.netuid = latest.netuid AND s.block = latest.max_block""",
            (anchor_ts,),
        )
        anchor_rows = await cursor.fetchall()

        holdings: dict[int, float] = {}
        eligible: list[tuple[int, float, float]] = []
        for r in anchor_rows:
            n = int(r["netuid"])
            if n in exclude:
                continue
            tao_in = float(r["tao_in"] or 0.0)
            price = float(r["alpha_price_tao"] or 0.0)
            if tao_in < min_pool_depth_tao or price <= 0:
                continue
            eligible.append((n, tao_in, price))

        if not eligible:
            return [initial_capital_tao for _ in timestamps], []

        total_tao_in = sum(t for _, t, _ in eligible)
        for n, t, p in eligible:
            weight = t / total_tao_in
            holdings[n] = (initial_capital_tao * weight) / p

        netuids = list(holdings.keys())
        placeholders = ",".join("?" * len(netuids))

        # Pull all relevant subnet snapshots from anchor to now in one go.
        cursor = await self._db.execute(
            f"""SELECT netuid, timestamp, alpha_price_tao
                FROM subnet_snapshots
                WHERE netuid IN ({placeholders})
                  AND datetime(timestamp) >= datetime(?)
                ORDER BY netuid ASC, datetime(timestamp) ASC""",
            (*netuids, anchor_ts),
        )
        rows = await cursor.fetchall()

        by_netuid: dict[int, list[tuple[str, float]]] = {n: [] for n in netuids}
        for r in rows:
            n = int(r["netuid"])
            if n in by_netuid:
                by_netuid[n].append((r["timestamp"], float(r["alpha_price_tao"] or 0.0)))

        values: list[float] = []
        for ts in timestamps:
            total = 0.0
            for n, alpha_amt in holdings.items():
                seq = by_netuid.get(n)
                if not seq:
                    continue
                lo, hi = 0, len(seq)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if seq[mid][0] <= ts:
                        lo = mid + 1
                    else:
                        hi = mid
                idx = lo - 1
                if idx < 0:
                    price = seq[0][1]
                else:
                    price = seq[idx][1]
                total += alpha_amt * price
            values.append(total)
        return values, netuids

    async def load_recent_snapshots(
        self, hours: int = 720, limit_per_netuid: int = 5000
    ) -> dict[int, list[dict]]:
        """Pull recent ``subnet_snapshots`` rows grouped by netuid. Used
        by the paper trader to seed its feature buffer each cycle."""
        cursor = await self._db.execute(
            """SELECT netuid, block, timestamp, alpha_price_tao, tao_price_usd,
                      tao_in, alpha_in, total_stake, emission_rate,
                      validator_count, neuron_count
               FROM subnet_snapshots
               WHERE datetime(timestamp) >= datetime('now', ?)
               ORDER BY netuid ASC, block ASC""",
            (f"-{hours} hours",),
        )
        rows = await cursor.fetchall()
        out: dict[int, list[dict]] = {}
        for r in rows:
            d = dict(r)
            n = int(d["netuid"])
            bucket = out.setdefault(n, [])
            if len(bucket) >= limit_per_netuid:
                continue
            bucket.append(d)
        return out

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
