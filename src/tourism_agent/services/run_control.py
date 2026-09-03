"""提供基于 thread_id 的进程内运行互斥与取消能力。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar, cast

ResultT = TypeVar("ResultT")


class ThreadBusyError(RuntimeError):
    """表示同一 thread_id 已经存在执行中的任务。"""


class ThreadRunCancelledError(RuntimeError):
    """表示当前 API 请求对应的图运行已被取消。"""


@dataclass
class _ActiveRun:
    """记录活动任务，以及允许取消图运行的时点。"""

    task: asyncio.Task[object]
    cancellable: asyncio.Event


class ThreadRunCoordinator:
    """用 thread_id 级别的锁保护图运行，不额外保存业务状态枚举。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_runs: dict[str, _ActiveRun] = {}

    async def execute(
        self,
        thread_id: str,
        operation: Callable[[], Awaitable[ResultT]],
        *,
        before_cancellation: Callable[[], Awaitable[None]] | None = None,
    ) -> ResultT:
        """运行一次操作；同一 thread_id 正在执行时直接拒绝新操作。"""
        task = await self.start(
            thread_id,
            operation,
            before_cancellation=before_cancellation,
        )
        try:
            return await task
        except asyncio.CancelledError as exc:
            raise ThreadRunCancelledError(f"thread_id={thread_id} 的运行已取消") from exc

    async def start(
        self,
        thread_id: str,
        operation: Callable[[], Awaitable[ResultT]],
        *,
        before_cancellation: Callable[[], Awaitable[None]] | None = None,
        on_failure: Callable[[BaseException], Awaitable[None]] | None = None,
    ) -> asyncio.Task[ResultT]:
        """占用 thread 锁后启动后台任务，并允许调用方收束异常终态。"""
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        if lock.locked():
            raise ThreadBusyError(f"thread_id={thread_id} 当前正在运行")

        await lock.acquire()
        cancellable = asyncio.Event()

        async def run() -> ResultT:
            try:
                try:
                    if before_cancellation is not None:
                        await before_cancellation()
                finally:
                    # 用户输入持久化成功或失败后，取消才可以继续处理图任务。
                    cancellable.set()
                return await operation()
            except asyncio.CancelledError as error:
                if on_failure is not None:
                    await on_failure(error)
                raise
            except Exception as error:
                if on_failure is not None:
                    await on_failure(error)
                raise
            finally:
                self._active_runs.pop(thread_id, None)
                lock.release()

        task = asyncio.create_task(run())
        self._active_runs[thread_id] = _ActiveRun(
            task=cast(asyncio.Task[object], task),
            cancellable=cancellable,
        )
        return task

    async def cancel(
        self,
        thread_id: str,
        finalize: Callable[[bool], Awaitable[ResultT]],
    ) -> ResultT:
        """取消活动任务，并在同一把锁内完成调用方的清理工作。"""
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        active_run = self._active_runs.get(thread_id)
        cancelled_running = active_run is not None
        if active_run is not None:
            await active_run.cancellable.wait()
            if not active_run.task.done():
                active_run.task.cancel()
                try:
                    await active_run.task
                except asyncio.CancelledError:
                    pass

        async with lock:
            return await finalize(cancelled_running)
