"""验证 API 运行控制只依赖 thread_id 对应的真实运行事实。"""

import asyncio

import pytest

from tourism_agent.services.run_control import (
    ThreadBusyError,
    ThreadRunCancelledError,
    ThreadRunCoordinator,
)


def test_same_thread_rejects_concurrent_execution() -> None:
    """同一 thread_id 的第二个运行不得越过互斥边界。"""

    async def scenario() -> None:
        coordinator = ThreadRunCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_operation() -> str:
            started.set()
            await release.wait()
            return "done"

        first = asyncio.create_task(coordinator.execute("thread-1", slow_operation))
        await started.wait()

        with pytest.raises(ThreadBusyError):
            await coordinator.execute("thread-1", slow_operation)

        release.set()
        assert await first == "done"

    asyncio.run(scenario())


def test_start_keeps_thread_locked_until_background_operation_finishes() -> None:
    """SSE 后台任务存活期间，同一 thread 不得启动第二次运行。"""

    async def scenario() -> None:
        coordinator = ThreadRunCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_operation() -> str:
            started.set()
            await release.wait()
            return "done"

        task = await coordinator.start("thread-1", slow_operation)
        await started.wait()
        with pytest.raises(ThreadBusyError):
            await coordinator.start("thread-1", slow_operation)

        release.set()
        assert await task == "done"
        assert await coordinator.execute("thread-1", lambda: asyncio.sleep(0, "next")) == "next"

    asyncio.run(scenario())


def test_start_reports_protected_input_failure_before_releasing_thread_lock() -> None:
    """SSE 的输入持久化失败也必须有统一收尾机会，不能留下活动会话。"""

    async def scenario() -> None:
        coordinator = ThreadRunCoordinator()
        failures: list[BaseException] = []

        async def persist_input() -> None:
            raise RuntimeError("模拟用户消息持久化失败")

        async def record_failure(error: BaseException) -> None:
            failures.append(error)

        task = await coordinator.start(
            "thread-1",
            lambda: asyncio.sleep(0),
            before_cancellation=persist_input,
            on_failure=record_failure,
        )
        with pytest.raises(RuntimeError, match="模拟用户消息持久化失败"):
            await task

        assert [str(error) for error in failures] == ["模拟用户消息持久化失败"]
        assert await coordinator.execute("thread-1", lambda: asyncio.sleep(0, "next")) == "next"

    asyncio.run(scenario())


def test_different_threads_can_execute_concurrently() -> None:
    """thread_id 级别的锁不能阻塞其他会话。"""

    async def scenario() -> None:
        coordinator = ThreadRunCoordinator()
        both_started = asyncio.Event()
        started: set[str] = set()

        async def operation(thread_id: str) -> str:
            started.add(thread_id)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            return thread_id

        results = await asyncio.gather(
            coordinator.execute("thread-1", lambda: operation("thread-1")),
            coordinator.execute("thread-2", lambda: operation("thread-2")),
        )

        assert results == ["thread-1", "thread-2"]

    asyncio.run(scenario())


def test_cancel_waits_until_active_thread_has_exited() -> None:
    """取消接口返回前，目标 thread 必须已经退出运行锁。"""

    async def scenario() -> None:
        coordinator = ThreadRunCoordinator()
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def operation() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        running = asyncio.create_task(coordinator.execute("thread-1", operation))
        await started.wait()

        async def finish_cancel(cancelled_running: bool) -> bool:
            return cancelled_running

        assert await coordinator.cancel("thread-1", finish_cancel) is True
        assert stopped.is_set()
        with pytest.raises(ThreadRunCancelledError):
            await running

    asyncio.run(scenario())


def test_cancel_finalizer_holds_thread_lock() -> None:
    """检查并清理 checkpoint 时，新的同 thread 运行必须被拒绝。"""

    async def scenario() -> None:
        coordinator = ThreadRunCoordinator()
        finalizer_started = asyncio.Event()
        release_finalizer = asyncio.Event()

        async def finalize(_cancelled_running: bool) -> bool:
            finalizer_started.set()
            await release_finalizer.wait()
            return True

        cancelling = asyncio.create_task(coordinator.cancel("thread-1", finalize))
        await finalizer_started.wait()

        with pytest.raises(ThreadBusyError):
            await coordinator.execute("thread-1", lambda: asyncio.sleep(0))

        release_finalizer.set()
        assert await cancelling is True

    asyncio.run(scenario())


def test_cancel_waits_for_protected_input_persistence() -> None:
    """取消不得打断已接受用户输入的持久化阶段。"""

    async def scenario() -> None:
        coordinator = ThreadRunCoordinator()
        persistence_started = asyncio.Event()
        release_persistence = asyncio.Event()
        persistence_completed = asyncio.Event()

        async def persist_input() -> None:
            persistence_started.set()
            await release_persistence.wait()
            persistence_completed.set()

        async def graph_operation() -> None:
            await asyncio.Event().wait()

        running = asyncio.create_task(
            coordinator.execute(
                "thread-1",
                graph_operation,
                before_cancellation=persist_input,
            )
        )
        await persistence_started.wait()

        async def finish_cancel(cancelled_running: bool) -> bool:
            return cancelled_running

        cancelling = asyncio.create_task(coordinator.cancel("thread-1", finish_cancel))
        await asyncio.sleep(0)
        assert cancelling.done() is False

        release_persistence.set()
        assert await cancelling is True
        assert persistence_completed.is_set()
        with pytest.raises(ThreadRunCancelledError):
            await running

    asyncio.run(scenario())
