"""Tiny async job queue for the bot.

The pipeline does heavy work (multi-GB downloads, archive extraction,
cookie parsing) so we never want more than ``concurrency`` jobs running
at once even if a dozen users submit at the same time. Jobs that can't
start immediately wait in a FIFO queue; ``/queue`` reports who's up
next.

Design notes
------------
* Pure asyncio. Each ``submit`` returns a :class:`Job` whose ``task``
  the caller can ``await`` for the final result (or cancel).
* Concurrency is enforced with an ``asyncio.Semaphore``.
* ``Job.state`` flips QUEUED → RUNNING → DONE/FAILED/CANCELLED.
* ``cancel`` removes pending jobs from the queue (no work was started)
  and asks the asyncio task to stop on already-running jobs (the
  blocking thread keeps going — Python doesn't expose a way to pre-empt
  threads — but its result is discarded).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


JobRunner = Callable[["Job"], Awaitable[None]]
"""``await runner(job)`` performs the actual work for the job."""


@dataclass
class Job:
    id: int
    user_id: int
    chat_id: int
    label: str
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    state: JobState = JobState.QUEUED
    error: Optional[str] = None
    task: Optional[asyncio.Task] = None  # populated by JobQueue.submit

    def is_terminal(self) -> bool:
        return self.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


class JobQueue:
    """FIFO queue with bounded concurrency."""

    def __init__(self, concurrency: int = 1) -> None:
        self.concurrency: int = max(1, int(concurrency))
        self._slot = asyncio.Semaphore(self.concurrency)
        self._lock = asyncio.Lock()
        self._jobs: Dict[int, Job] = {}
        self._pending: List[int] = []  # FIFO of queued job ids
        self._running: List[int] = []  # currently RUNNING job ids
        self._next_id = 0

    # ------------------------------------------------------------------
    # Submission / cancellation
    # ------------------------------------------------------------------
    async def submit(
        self,
        *,
        user_id: int,
        chat_id: int,
        label: str,
        runner: JobRunner,
    ) -> Job:
        async with self._lock:
            self._next_id += 1
            job = Job(
                id=self._next_id,
                user_id=int(user_id),
                chat_id=int(chat_id),
                label=label,
            )
            self._jobs[job.id] = job
            self._pending.append(job.id)
        job.task = asyncio.create_task(self._driver(job, runner))
        return job

    async def cancel(self, job_id: int) -> bool:
        """Cancel a queued or running job. Returns True if cancelled."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal():
                return False
            if job_id in self._pending:
                self._pending.remove(job_id)
                job.state = JobState.CANCELLED
                job.finished_at = time.time()
                if job.task is not None:
                    job.task.cancel()
                return True
            # Running: ask the task to stop. The blocking work in the
            # executor keeps going but its return value is discarded.
            if job_id in self._running:
                if job.task is not None:
                    job.task.cancel()
                # The driver will flip state to CANCELLED in its
                # finally block; reflect that eagerly here so a
                # follow-up /queue shows the right status.
                job.state = JobState.CANCELLED
                return True
        return False

    async def cancel_user_jobs(self, user_id: int) -> int:
        """Cancel every active job belonging to ``user_id``."""
        ids: List[int] = []
        async with self._lock:
            for job in self._jobs.values():
                if job.user_id == int(user_id) and not job.is_terminal():
                    ids.append(job.id)
        n = 0
        for jid in ids:
            if await self.cancel(jid):
                n += 1
        return n

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    async def position(self, job_id: int) -> int:
        """Position of ``job_id`` in the waiting line.

        ``0`` while the job is running. ``1`` means "next up after the
        current running batch finishes"; ``N`` means there are ``N-1``
        jobs ahead of you in the waiting line. Returns ``-1`` if the
        job is unknown or already terminal.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal():
                return -1
            if job_id in self._running:
                return 0
            try:
                idx = self._pending.index(job_id)
            except ValueError:
                return -1
            return idx + 1

    async def snapshot(self) -> Dict[str, List[Job]]:
        """Return shallow lists of ``running`` and ``pending`` jobs."""
        async with self._lock:
            return {
                "running": [self._jobs[j] for j in self._running],
                "pending": [self._jobs[j] for j in self._pending],
            }

    async def get_job(self, job_id: int) -> Optional[Job]:
        async with self._lock:
            return self._jobs.get(job_id)

    # ------------------------------------------------------------------
    # Internal driver
    # ------------------------------------------------------------------
    async def _driver(self, job: Job, runner: JobRunner) -> None:
        try:
            await self._slot.acquire()
        except asyncio.CancelledError:
            # Cancelled while waiting in queue.
            async with self._lock:
                if job.id in self._pending:
                    self._pending.remove(job.id)
                if not job.is_terminal():
                    job.state = JobState.CANCELLED
                    job.finished_at = time.time()
            return

        try:
            async with self._lock:
                # If we got cancelled in the gap between releasing the
                # lock and acquiring the semaphore, bail out cleanly.
                if job.id not in self._pending:
                    self._slot.release()
                    if not job.is_terminal():
                        job.state = JobState.CANCELLED
                        job.finished_at = time.time()
                    return
                self._pending.remove(job.id)
                self._running.append(job.id)
                job.state = JobState.RUNNING
                job.started_at = time.time()

            try:
                await runner(job)
            except asyncio.CancelledError:
                job.state = JobState.CANCELLED
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s failed", job.id)
                job.state = JobState.FAILED
                job.error = str(exc)
            else:
                job.state = JobState.DONE
            finally:
                async with self._lock:
                    if job.id in self._running:
                        self._running.remove(job.id)
                    job.finished_at = time.time()
                self._slot.release()
        except asyncio.CancelledError:
            return
