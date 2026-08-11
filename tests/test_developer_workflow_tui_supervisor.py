from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from src.developer_workflow.tui.models import RunActivity, TuiDisplayError
from src.developer_workflow.tui.supervisor import (
    RunTaskSupervisor,
    SupervisorClosedError,
    SupervisorLoopError,
    TaskEvent,
)


async def _wait_thread_event(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 2)


@pytest.mark.asyncio
async def test_same_run_is_fifo_without_occupying_another_global_slot() -> None:
    release_first = threading.Event()
    release_other = threading.Event()
    release_second = threading.Event()
    release_second.set()
    first_started = threading.Event()
    other_started = threading.Event()
    starts: list[str] = []
    starts_lock = threading.Lock()
    supervisor = RunTaskSupervisor(max_concurrency=2)

    def operation(name: str, started: threading.Event, release: threading.Event) -> str:
        with starts_lock:
            starts.append(name)
        started.set()
        assert release.wait(2)
        return name

    first = supervisor.submit(
        "run-a", "resume", lambda: operation("a1", first_started, release_first)
    )
    second = supervisor.submit(
        "run-a", "approve", lambda: operation("a2", threading.Event(), release_second)
    )
    other = supervisor.submit(
        "run-b", "resume", lambda: operation("b1", other_started, release_other)
    )

    await _wait_thread_event(first_started)
    await _wait_thread_event(other_started)
    assert starts == ["a1", "b1"]
    release_first.set()
    assert await first == "a1"
    assert await second == "a2"
    release_other.set()
    assert await other == "b1"
    await supervisor.close()


@pytest.mark.asyncio
async def test_max_concurrency_is_strict_and_same_run_order_is_fifo() -> None:
    release = threading.Event()
    all_started = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    order: list[int] = []
    supervisor = RunTaskSupervisor(max_concurrency=2)

    def operation(number: int) -> int:
        nonlocal active, peak
        with lock:
            order.append(number)
            active += 1
            peak = max(peak, active)
            if active == 2:
                all_started.set()
        assert release.wait(2)
        with lock:
            active -= 1
        return number

    same_run = [
        supervisor.submit(
            "ordered", f"step-{number}", lambda number=number: operation(number)
        )
        for number in range(3)
    ]
    others = [
        supervisor.submit(
            f"run-{number}", "resume", lambda number=number: operation(number + 10)
        )
        for number in range(3)
    ]
    await _wait_thread_event(all_started)
    assert peak == 2
    release.set()
    assert await asyncio.gather(*same_run) == [0, 1, 2]
    await asyncio.gather(*others)
    assert [item for item in order if item < 10] == [0, 1, 2]
    assert peak == 2
    await supervisor.close()


@pytest.mark.asyncio
async def test_readonly_calls_run_concurrently_but_obey_global_limit() -> None:
    release = threading.Event()
    two_started = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    supervisor = RunTaskSupervisor(max_concurrency=2)

    def read(value: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
        assert release.wait(2)
        with lock:
            active -= 1
        return value

    tasks = [
        asyncio.create_task(supervisor.run_readonly("query", read, number))
        for number in range(4)
    ]
    await _wait_thread_event(two_started)
    assert peak == 2
    release.set()
    assert await asyncio.gather(*tasks) == [0, 1, 2, 3]
    assert peak == 2
    await supervisor.close()


@pytest.mark.asyncio
async def test_events_are_fixed_and_sink_failures_do_not_change_results() -> None:
    events: list[TaskEvent] = []

    def sink(event: TaskEvent) -> None:
        events.append(event)
        raise RuntimeError("TOKEN-SINK")

    supervisor = RunTaskSupervisor(max_concurrency=1, sink=sink)
    assert (
        await supervisor.run_mutation("run-a", "resume", lambda: "result")
        == "result"
    )

    error = RuntimeError("TOKEN-CALL")

    def fail() -> None:
        raise error

    with pytest.raises(RuntimeError) as caught:
        await supervisor.run_mutation("run-a", "approve", fail)
    assert caught.value is error
    assert [(event.activity, event.message) for event in events] == [
        (RunActivity.QUEUED, "workflow action queued"),
        (RunActivity.RUNNING, "workflow action started"),
        (RunActivity.IDLE, "workflow action completed"),
        (RunActivity.QUEUED, "workflow action queued"),
        (RunActivity.RUNNING, "workflow action started"),
        (RunActivity.IDLE, "workflow action failed safely"),
    ]
    assert "TOKEN" not in repr(events)
    await supervisor.close()


def test_task_event_is_frozen_safe_and_has_fixed_failure_message() -> None:
    event = TaskEvent.failed("run-a", RuntimeError("TOKEN-SECRET"))
    assert event.message == "workflow action failed safely"
    assert "TOKEN" not in repr(event)
    with pytest.raises(FrozenInstanceError):
        event.message = "changed"  # type: ignore[misc]
    with pytest.raises(TuiDisplayError, match="^display value is invalid$"):
        TaskEvent.queued("unsafe\nTOKEN", "resume")
    with pytest.raises(TuiDisplayError, match="^display value is invalid$"):
        TaskEvent.queued("run-a", "unsafe\nTOKEN")
    with pytest.raises(TuiDisplayError, match="^display value is invalid$"):
        TaskEvent(
            "run-a",
            "resume",
            RunActivity.QUEUED,
            "workflow action completed",
        )


@pytest.mark.asyncio
async def test_close_cancels_waiters_detaches_running_work_and_is_idempotent() -> None:
    running_started = threading.Event()
    running_finished = threading.Event()
    release = threading.Event()
    business_cancel_called = threading.Event()
    events: list[TaskEvent] = []
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda unused_loop, context: loop_errors.append(context))
    supervisor = RunTaskSupervisor(max_concurrency=1, sink=events.append)

    class BusinessOperation:
        def __call__(self) -> str:
            running_started.set()
            assert release.wait(2)
            running_finished.set()
            raise RuntimeError("TOKEN-DETACHED")

        def cancel(self) -> None:
            business_cancel_called.set()

    try:
        running = supervisor.submit("run-a", "resume", BusinessOperation())
        queued = supervisor.submit("run-b", "approve", lambda: "never")
        await _wait_thread_event(running_started)
        started = time.monotonic()
        await supervisor.close()
        assert time.monotonic() - started < 0.2
        assert queued.cancelled()
        assert running.cancelled()
        assert supervisor.task_count == 0
        assert supervisor.run_lock_count == 0
        assert not business_cancel_called.is_set()
        assert events[-1].message == "workflow action cancelled"
        assert supervisor.closed
        await supervisor.close()
        release.set()
        await _wait_thread_event(running_finished)
        await asyncio.sleep(0.05)
        assert supervisor.task_count == 0
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_close_rejects_new_work_and_cleans_per_run_locks() -> None:
    supervisor = RunTaskSupervisor(max_concurrency=1)
    assert await supervisor.run_mutation("run-a", "resume", lambda: 1) == 1
    await asyncio.sleep(0)
    assert supervisor.run_lock_count == 0
    await supervisor.close()
    with pytest.raises(
        SupervisorClosedError, match="^workflow task supervisor is closed$"
    ):
        supervisor.submit("run-b", "resume", lambda: 2)


@pytest.mark.asyncio
async def test_immediate_cancel_has_fixed_event_and_cleans_lock() -> None:
    events: list[TaskEvent] = []
    supervisor = RunTaskSupervisor(max_concurrency=1, sink=events.append)
    task = supervisor.submit("run-a", "resume", lambda: "never")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert [event.message for event in events] == [
        "workflow action queued",
        "workflow action cancelled",
    ]
    assert supervisor.run_lock_count == 0
    await supervisor.close()


@pytest.mark.parametrize("value", [True, False, 0, 9, -1, 1.0, "2", None])
def test_max_concurrency_requires_exact_int_in_range(value: object) -> None:
    with pytest.raises(ValueError, match="^max_concurrency must be between 1 and 8$"):
        RunTaskSupervisor(value)  # type: ignore[arg-type]


def test_constructor_requires_a_running_loop() -> None:
    with pytest.raises(
        SupervisorLoopError,
        match="^workflow task supervisor requires its running event loop$",
    ):
        RunTaskSupervisor(1)


@pytest.mark.asyncio
async def test_submit_from_a_different_thread_has_a_fixed_error() -> None:
    supervisor = RunTaskSupervisor(1)
    errors: list[BaseException] = []

    def submit_elsewhere() -> None:
        try:
            supervisor.submit("run-a", "resume", lambda: None)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=submit_elsewhere)
    thread.start()
    thread.join()
    assert len(errors) == 1
    assert type(errors[0]) is SupervisorLoopError
    assert str(errors[0]) == "workflow task supervisor requires its running event loop"
    await supervisor.close()
