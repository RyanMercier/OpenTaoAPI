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
"""


class Database:
    def __init__(self, db_path: str):
        self._path = db_path
        self._db: Optional[aiosqlite.Connection] = None

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
        """Insert a snapshot row. Returns True if inserted, False if duplicate."""
        try:
            await self._db.execute(
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
            return self._db.total_changes > 0
        except Exception as e:
            logger.error(f"Failed to insert snapshot: {e}")
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
        """Get alpha price history for a subnet over the last N hours."""
        cursor = await self._db.execute(
            """SELECT block, timestamp, alpha_price_tao, tao_price_usd
               FROM subnet_snapshots
               WHERE netuid = ?
                 AND timestamp >= datetime('now', ?)
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
                 AND timestamp >= datetime('now', ?)
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

    async def get_snapshot_count(self) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM subnet_snapshots"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
