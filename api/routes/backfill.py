from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.services.backfill_jobs import BackfillJobs

router = APIRouter(tags=["backfill"])

_jobs: Optional[BackfillJobs] = None


def init_backfill_router(jobs: BackfillJobs):
    global _jobs
    _jobs = jobs


def _require_jobs() -> BackfillJobs:
    if _jobs is None:
        raise HTTPException(status_code=503, detail="Backfill service not initialised")
    return _jobs


def _serialize(job) -> dict:
    return {
        "netuid": job.netuid,
        "days": job.days,
        "state": job.state,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "inserted": job.inserted,
        "error": job.error,
    }


@router.post(
    "/subnet/{netuid}/backfill",
    summary="Start an on-demand archive-node backfill for one subnet",
)
async def start_backfill(
    netuid: int,
    days: int = Query(30, ge=1, le=365),
):
    """Kick off a background backfill for ``netuid`` covering the last
    ``days`` days at epoch resolution, fetched from the public archive
    node. If another backfill for the same subnet is already running,
    the existing job is returned (no duplicate archive connections).

    Rows land in SQLite as they arrive; poll
    ``GET /subnet/{netuid}/backfill`` to track progress, and refetch
    ``/subnet/{netuid}/candles`` when ``state == "done"``."""
    job, started = await _require_jobs().start(netuid, days)
    return {**_serialize(job), "started": started}


@router.get(
    "/subnet/{netuid}/backfill",
    summary="Check status of an on-demand backfill",
)
async def get_backfill(netuid: int):
    job = _require_jobs().get(netuid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No backfill recorded for SN{netuid}")
    return _serialize(job)
