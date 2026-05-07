"""异步任务队列 - asyncio.Queue + Task"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

import structlog

log = structlog.get_logger()


class TaskQueue:
    """异步任务队列，带 worker 消费

    用法:
        queue = TaskQueue(max_workers=3)
        queue.start()
        await queue.enqueue(my_coroutine)
        await queue.stop()
    """

    def __init__(self, max_workers: int = 3):
        self._queue: asyncio.Queue[Coroutine] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._max_workers = max_workers
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for i in range(self._max_workers):
            t = asyncio.create_task(self._worker(i))
            self._workers.append(t)
        log.info("queue_started", workers=self._max_workers)

    async def stop(self) -> None:
        self._running = False
        for _ in self._workers:
            await self._queue.put(self._sentinel())
        for t in self._workers:
            t.cancel()
        self._workers.clear()
        log.info("queue_stopped")

    async def enqueue(self, coro: Coroutine) -> None:
        await self._queue.put(coro)
        log.debug("queue_enqueue", queue_size=self._queue.qsize())

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def _worker(self, worker_id: int) -> None:
        while self._running:
            try:
                coro = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                continue
            try:
                await coro
            except Exception as e:
                log.error("queue_task_failed", worker=worker_id, error=str(e))
            finally:
                self._queue.task_done()

    @staticmethod
    async def _sentinel():
        pass
