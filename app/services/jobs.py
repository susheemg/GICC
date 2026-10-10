"""Job runner - GICC-07 §2.2.

Long-running work with per-item state, so a restart resumes rather than
restarts. No broker and no Redis: the platform runs as a single deployable
unit, and adding infrastructure to classify fifty documents is not warranted.

Two rules shape it:
  * one item's exception marks that item failed and the job continues — a job
    that fails wholesale is a bug, not an outcome
  * job status is derived from its items, never held independently, so the two
    cannot disagree
"""
from __future__ import annotations

import asyncio
import json
import traceback
from typing import Callable

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..config import JOB_CONCURRENCY, MAX_AI_CALLS_PER_JOB
from ..db import SessionLocal
from ..models import Job, JobItem, now

# kind -> handler(db, job, item, payload) -> dict
_HANDLERS: dict[str, Callable] = {}


def register(kind: str, handler: Callable) -> None:
    _HANDLERS[kind] = handler


def create(db: Session, *, kind: str, tenant_id: str | None, items: list[dict],
           payload: dict | None = None, created_by: str | None = None) -> Job:
    job = Job(kind=kind, tenant_id=tenant_id, total=len(items),
              payload_json=json.dumps(payload or {}, default=str),
              created_by=created_by, status="pending")
    db.add(job)
    db.flush()
    for position, item in enumerate(items, start=1):
        db.add(JobItem(job_id=job.id, position=position,
                       ref=str(item.get("ref", ""))[:250],
                       result_json=json.dumps(item, default=str)))
    db.flush()
    return job


def progress(db: Session, job_id: str) -> dict | None:
    job = db.get(Job, job_id)
    if job is None:
        return None
    counts = dict(db.execute(
        select(JobItem.status, func.count(JobItem.id))
        .where(JobItem.job_id == job.id).group_by(JobItem.status)).all())
    done = counts.get("done", 0)
    failed = counts.get("failed", 0)
    settled = done + failed
    return {
        "id": job.id, "kind": job.kind, "status": job.status,
        "total": job.total, "done": done, "failed": failed,
        "pending": job.total - settled,
        "percent": round(settled / job.total * 100) if job.total else 100,
        "finished": job.status in {"complete", "failed", "cancelled"},
        "result": json.loads(job.result_json) if job.result_json else None,
        "error": job.error,
        "ai_calls": job.ai_calls,
    }


def items(db: Session, job_id: str) -> list[JobItem]:
    return list(db.scalars(select(JobItem).where(JobItem.job_id == job_id)
                           .order_by(JobItem.position)).all())


async def run(job_id: str) -> None:
    """Executes a job's pending items with bounded concurrency."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None or job.status in {"complete", "cancelled"}:
            return
        handler = _HANDLERS.get(job.kind)
        if handler is None:
            job.status = "failed"
            job.error = f"No handler registered for job kind '{job.kind}'"
            job.finished_at = now()
            db.commit()
            return
        job.status = "running"
        job.started_at = job.started_at or now()
        db.commit()

    semaphore = asyncio.Semaphore(JOB_CONCURRENCY)

    async def run_item(item_id: str) -> None:
        async with semaphore:
            await asyncio.to_thread(_execute_item, job_id, item_id)

    with SessionLocal() as db:
        pending = [i.id for i in db.scalars(select(JobItem).where(and_(
            JobItem.job_id == job_id, JobItem.status.in_(["pending", "running"])))
            .order_by(JobItem.position)).all()]

    await asyncio.gather(*(run_item(i) for i in pending), return_exceptions=True)
    await asyncio.to_thread(_finalise, job_id)


def _execute_item(job_id: str, item_id: str) -> None:
    """Runs one item in its own session, so a failure cannot poison the
    transaction of the items running beside it."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        item = db.get(JobItem, item_id)
        if job is None or item is None or item.status == "done":
            return
        if job.status == "cancelled":
            return

        handler = _HANDLERS.get(job.kind)
        payload = json.loads(job.payload_json) if job.payload_json else {}
        item_payload = json.loads(item.result_json) if item.result_json else {}
        item.status = "running"
        item.attempts += 1
        db.commit()

        try:
            if job.ai_calls >= MAX_AI_CALLS_PER_JOB:
                item_payload["_ai_budget_exhausted"] = True
            result = handler(db, job, item, {**payload, **item_payload})
            item.status = "done"
            item.result_json = json.dumps(result or {}, default=str)[:8000]
            item.error = None
            db.commit()
        except Exception as exc:
            db.rollback()
            item = db.get(JobItem, item_id)
            item.status = "failed"
            item.error = f"{type(exc).__name__}: {exc}"[:900]
            db.commit()
            print(f"[job {job_id}] item {item.position} failed: {exc}", flush=True)
            traceback.print_exc()


def _finalise(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        counts = dict(db.execute(
            select(JobItem.status, func.count(JobItem.id))
            .where(JobItem.job_id == job.id).group_by(JobItem.status)).all())
        job.done = counts.get("done", 0)
        job.failed = counts.get("failed", 0)
        job.status = "complete"
        job.finished_at = now()
        db.commit()

        finaliser = _HANDLERS.get(f"{job.kind}:finalise")
        if finaliser is not None:
            try:
                result = finaliser(db, job)
                job.result_json = json.dumps(result or {}, default=str)[:8000]
                db.commit()
            except Exception as exc:
                db.rollback()
                job = db.get(Job, job_id)
                job.error = f"finalise failed: {type(exc).__name__}: {exc}"[:900]
                db.commit()
                print(f"[job {job_id}] finalise failed: {exc}", flush=True)


def start(job_id: str) -> None:
    """Schedules a job on the running loop. Falls back to synchronous execution
    where there is no loop, so tests and scripts work without ceremony."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run(job_id))
        return
    loop.create_task(run(job_id))


def note_ai_call(db: Session, job: Job, count: int = 1) -> None:
    job.ai_calls += count
    db.flush()


def resume_incomplete() -> int:
    """Called at startup. A job left running by a restart resumes from its
    pending items."""
    with SessionLocal() as db:
        stale = list(db.scalars(select(Job).where(
            Job.status.in_(["pending", "running"]))).all())
        ids = [j.id for j in stale]
    for job_id in ids:
        start(job_id)
    return len(ids)
