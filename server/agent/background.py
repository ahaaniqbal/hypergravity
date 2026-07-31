"""Work that outlives the call.

The point of the whole build: you ring up, ask for something slow, and hang up.
The agent keeps going without you and texts you when it is done. The loop closes
after the conversation ends, which is the only way a voice assistant is useful
for anything that takes longer than a person is willing to hold the phone.

Two things make this harder than "spawn a task":

The call session is torn down on hangup — its pipeline, its counterparty client,
its whole context. So a background job cannot borrow any of that. It gets its own
counterparty and reads the phone number it was given at the time it was started.

And the honesty rule still applies, with nobody listening. The text we send has
to say what actually happened, including "this failed", which is exactly the
moment it would be easiest to send a cheerful nothing instead.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

from . import events
from .counterparty import Counterparty, sms_delivered as _sms_delivered
from .ledger import StepState, get_ledger

MAX_SMS_BODY = 300


@dataclass
class Job:
    job_id: str
    what: str
    task_id: str
    notify: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    ok: bool | None = None
    result: str = ""
    notified: bool = False

    @property
    def running(self) -> bool:
        # Not done until the caller has actually been told. Reporting completion
        # at the moment the work finished let the process tear down with the
        # text still pending — a result nobody ever received.
        return self.finished_at is None or not self.notified

    @property
    def elapsed(self) -> int:
        return int((self.finished_at or time.time()) - self.started_at)


_JOBS: dict[str, Job] = {}
_TASKS: set[asyncio.Task] = set()


def jobs() -> dict[str, Job]:
    return _JOBS


def running_jobs(task_id: str = "") -> list[Job]:
    return [
        j for j in _JOBS.values()
        if j.running and (not task_id or j.task_id == task_id)
    ]


def _phrase(job: Job) -> str:
    """The text message. Short, spoken-sounding, and honest about failure."""
    took = f"{job.elapsed}s" if job.elapsed < 90 else f"{job.elapsed // 60} min"
    head = f"Done — {job.what}" if job.ok else f"Couldn't finish — {job.what}"
    body = (job.result or "").strip().replace("\n", " ")
    if len(body) > MAX_SMS_BODY:
        body = body[:MAX_SMS_BODY] + "…"
    return f"{head} ({took}).\n\n{body}" if body else f"{head} ({took})."


async def _notify(job: Job) -> None:
    """Text the result. Its own counterparty: the call's was closed at hangup."""
    if not job.notify:
        logger.warning(f"job {job.job_id} finished with nowhere to send the result")
        job.notified = True
        return
    cp = Counterparty()
    try:
        body = _phrase(job)
        resp = await cp.send_confirmation_sms(to=job.notify, body=body)
        # Delivery must be positively evidenced. "No known error word in a
        # field that is usually absent" is not evidence of anything.
        if not _sms_delivered(resp):
            logger.error(f"job {job.job_id} could not text {job.notify}: {resp}")
        else:
            logger.info(f"job {job.job_id} texted {job.notify}: {body[:60]}")
            get_ledger(job.task_id).record_evidence(
                "sms", job.notify, {"to": job.notify, "body": body}
            )
    except Exception as e:  # noqa: BLE001 — a failed text must not lose the result
        logger.error(f"job {job.job_id} notify failed: {e}")
    finally:
        job.notified = True
        await cp.aclose()


async def _supervise(job: Job, work: Callable[[], Awaitable[Any]]) -> None:
    ledger = get_ledger(job.task_id)
    label = f"background: {job.what[:32]}"
    try:
        result = await work()
        job.ok = True
        job.result = str(result)
        ledger.mark(label, StepState.DONE)
    except Exception as e:  # noqa: BLE001 — report, never swallow
        job.ok = False
        job.result = str(e)
        ledger.mark(label, StepState.FAILED, str(e)[:60])
        logger.error(f"job {job.job_id} failed: {e}")
    finally:
        job.finished_at = time.time()
        events.step(label, "done" if job.ok else "failed", job.result[:60])
        events.state("idle", f"texted the result of: {job.what}")
        await _notify(job)


def start(what: str, task_id: str, notify: str, work: Callable[[], Awaitable[Any]]) -> Job:
    """Kick off work and return immediately, so the caller can hang up.

    The task is held in a module-level set: asyncio only keeps a weak reference,
    and a garbage-collected task is a job that silently never finishes.
    """
    job_id = f"job-{len(_JOBS) + 1}-{int(time.time()) % 10000}"
    job = Job(job_id=job_id, what=what, task_id=task_id, notify=notify)
    _JOBS[job_id] = job

    task = asyncio.create_task(_supervise(job, work))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    logger.info(f"started {job_id}: {what}")
    events.step(f"background: {what[:32]}", "pending", "running after the call")
    return job
