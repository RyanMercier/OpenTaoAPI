"""In-process backfill job queue.

The subnet detail page triggers these when its chart is sparse. One active
job per subnet — a second request while the first is running returns the
existing job instead of starting a second archive-node connection.

Jobs are ephemeral. They live in-memory and are garbage-collected on
process exit, which is fine: the data they wrote is durably in SQLite, and
the UI only needs to poll while the page is open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from api.config import settings
from api.services.calculations import BLOCKS_PER_DAY
from api.services.database import Database

logger = logging.getLogger(__name__)


@dataclass
class BackfillJob:
    netuid: int
    days: int
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    inserted: int = 0
    state: str = "running"   # running | done | failed
    error: Optional[str] = None


class BackfillJobs:
    def __init__(self, db: Database):
        self._db = db
        self._jobs: dict[int, BackfillJob] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, netuid: int) -> Optional[BackfillJob]:
        return self._jobs.get(netuid)

    def _lock_for(self, netuid: int) -> asyncio.Lock:
        lock = self._locks.get(netuid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[netuid] = lock
        return lock

    async def start(self, netuid: int, days: int) -> tuple[BackfillJob, bool]:
        """Return (job, started_new). If a job for this subnet is already
        running, reuses it."""
        lock = self._lock_for(netuid)
        # `locked()` is enough — we only want to reject while another job
        # is mid-run, and acquiring the lock is handled inside the task.
        existing = self._jobs.get(netuid)
        if existing and existing.state == "running":
            return existing, False

        job = BackfillJob(netuid=netuid, days=max(1, min(days, 365)))
        self._jobs[netuid] = job
        asyncio.create_task(self._run(job, lock))
        return job, True

    async def _run(self, job: BackfillJob, lock: asyncio.Lock) -> None:
        from bittensor.core.async_subtensor import AsyncSubtensor
        from scripts.backfill import backfill_subnet

        async with lock:
            # Revalidate state under the lock — another task may have
            # already satisfied this request between start() and _run().
            if job.state != "running":
                return
            try:
                network = settings.archive_endpoint
                async with AsyncSubtensor(network=network) as subtensor:
                    current_block = await subtensor.get_current_block()
                    start_block = current_block - (BLOCKS_PER_DAY * job.days)
                    inserted = await backfill_subnet(
                        subtensor=subtensor,
                        db=self._db,
                        netuid=job.netuid,
                        start=start_block,
                        end=current_block,
                        step=360,   # epoch resolution
                        full=False,
                        delay=0.3,
                    )
                job.inserted = inserted
                job.state = "done"
                logger.info(
                    "Backfill SN%d complete: +%d rows in %.1fs",
                    job.netuid, inserted, time.time() - job.started_at,
                )
            except Exception as exc:
                job.state = "failed"
                job.error = str(exc)
                logger.exception("Backfill SN%d failed", job.netuid)
            finally:
                job.finished_at = time.time()
