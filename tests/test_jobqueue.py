"""Tests for the bot's lightweight async JobQueue."""

from __future__ import annotations

import asyncio

import pytest

from pipeline.jobqueue import JobQueue, JobState


@pytest.mark.asyncio
async def test_runs_jobs_in_order_at_concurrency_one() -> None:
    q = JobQueue(concurrency=1)
    order: list[int] = []

    async def make_runner(label: int):
        async def _run(_job):
            await asyncio.sleep(0.01)
            order.append(label)

        return _run

    j1 = await q.submit(user_id=1, chat_id=1, label="a", runner=await make_runner(1))
    j2 = await q.submit(user_id=2, chat_id=2, label="b", runner=await make_runner(2))
    j3 = await q.submit(user_id=3, chat_id=3, label="c", runner=await make_runner(3))

    await asyncio.gather(j1.task, j2.task, j3.task)
    assert order == [1, 2, 3]
    assert j1.state == JobState.DONE
    assert j2.state == JobState.DONE
    assert j3.state == JobState.DONE


@pytest.mark.asyncio
async def test_concurrency_two_overlaps_jobs() -> None:
    q = JobQueue(concurrency=2)
    started_at: dict[int, float] = {}
    finished: list[int] = []

    async def runner_factory(jid: int):
        async def _run(_job):
            started_at[jid] = asyncio.get_event_loop().time()
            await asyncio.sleep(0.05)
            finished.append(jid)

        return _run

    jobs = []
    for i in range(3):
        runner = await runner_factory(i)
        jobs.append(
            await q.submit(user_id=i, chat_id=i, label=str(i), runner=runner)
        )

    await asyncio.gather(*[j.task for j in jobs])
    # First two should start within ~milliseconds of each other; the
    # third only after one of them finishes.
    assert abs(started_at[0] - started_at[1]) < 0.02
    assert started_at[2] - started_at[0] >= 0.04


@pytest.mark.asyncio
async def test_position_reports_running_zero() -> None:
    q = JobQueue(concurrency=1)
    gate = asyncio.Event()

    async def slow_runner(_job):
        await gate.wait()

    j1 = await q.submit(
        user_id=1, chat_id=1, label="slow", runner=slow_runner
    )
    j2 = await q.submit(
        user_id=2, chat_id=2, label="next", runner=slow_runner
    )

    # Yield enough times for the driver to acquire the semaphore.
    for _ in range(10):
        await asyncio.sleep(0)
        if (await q.position(j1.id)) == 0:
            break

    assert (await q.position(j1.id)) == 0
    assert (await q.position(j2.id)) == 1

    gate.set()
    await asyncio.gather(j1.task, j2.task)


@pytest.mark.asyncio
async def test_cancel_pending_job() -> None:
    q = JobQueue(concurrency=1)
    gate = asyncio.Event()
    ran: list[int] = []

    async def slow_runner(job):
        await gate.wait()
        ran.append(job.id)

    j1 = await q.submit(user_id=1, chat_id=1, label="head", runner=slow_runner)
    j2 = await q.submit(user_id=2, chat_id=2, label="tail", runner=slow_runner)

    cancelled = await q.cancel(j2.id)
    assert cancelled
    gate.set()
    await j1.task
    # j2's task got cancelled; awaiting it should not raise here because
    # the driver swallows CancelledError gracefully.
    if j2.task is not None:
        with pytest.raises(asyncio.CancelledError):
            await j2.task
    assert ran == [j1.id]
    assert j2.state == JobState.CANCELLED


@pytest.mark.asyncio
async def test_cancel_user_jobs_drops_all_pending() -> None:
    q = JobQueue(concurrency=1)
    gate = asyncio.Event()

    async def slow_runner(_job):
        await gate.wait()

    head = await q.submit(user_id=99, chat_id=1, label="head", runner=slow_runner)
    j1 = await q.submit(user_id=42, chat_id=2, label="a", runner=slow_runner)
    j2 = await q.submit(user_id=42, chat_id=3, label="b", runner=slow_runner)
    j3 = await q.submit(user_id=7, chat_id=4, label="other", runner=slow_runner)

    n = await q.cancel_user_jobs(42)
    assert n == 2
    assert j1.state == JobState.CANCELLED
    assert j2.state == JobState.CANCELLED

    gate.set()
    await asyncio.gather(head.task, j3.task, return_exceptions=True)
    assert head.state == JobState.DONE
    assert j3.state == JobState.DONE


@pytest.mark.asyncio
async def test_failed_runner_marks_state_failed() -> None:
    q = JobQueue(concurrency=1)

    async def boom(_job):
        raise RuntimeError("kaboom")

    j = await q.submit(user_id=1, chat_id=1, label="x", runner=boom)
    await j.task
    assert j.state == JobState.FAILED
    assert "kaboom" in (j.error or "")


@pytest.mark.asyncio
async def test_snapshot_returns_running_and_pending() -> None:
    q = JobQueue(concurrency=1)
    gate = asyncio.Event()

    async def slow(_job):
        await gate.wait()

    j1 = await q.submit(user_id=1, chat_id=1, label="r", runner=slow)
    j2 = await q.submit(user_id=2, chat_id=2, label="p", runner=slow)

    for _ in range(10):
        await asyncio.sleep(0)
        if (await q.position(j1.id)) == 0:
            break

    snap = await q.snapshot()
    assert [j.id for j in snap["running"]] == [j1.id]
    assert [j.id for j in snap["pending"]] == [j2.id]

    gate.set()
    await asyncio.gather(j1.task, j2.task)
