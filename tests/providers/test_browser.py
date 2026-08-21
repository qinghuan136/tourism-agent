"""验证匿名 Playwright MCP Client 的隔离和安全边界。"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib import import_module
from typing import Any

import pytest
from langchain_core.tools import BaseTool, tool


async def public_resolver(_host: str, _port: int) -> list[str]:
    """将测试域名固定解析到公共示例地址。"""
    return ["93.184.216.34"]


def fake_browser_tools(
    respond: Callable[[str, dict[str, Any]], str],
) -> list[BaseTool]:
    """模拟真实 Playwright 白名单的完整名称和参数结构。"""
    @tool("browser_navigate")
    async def navigate(url: str) -> str:
        """模拟导航。"""
        return respond("browser_navigate", {"url": url})

    @tool("browser_snapshot")
    async def snapshot() -> str:
        """模拟页面快照。"""
        return respond("browser_snapshot", {})

    @tool("browser_find")
    async def find(text: str = "", regex: str = "") -> str:
        """模拟页面查找。"""
        return respond("browser_find", {"text": text, "regex": regex})

    @tool("browser_wait_for")
    async def wait_for(time: float = 0) -> str:
        """模拟页面等待。"""
        return respond("browser_wait_for", {"time": time})

    @tool("browser_navigate_back")
    async def navigate_back() -> str:
        """模拟返回上一页。"""
        return respond("browser_navigate_back", {})

    @tool("browser_tabs")
    async def tabs(action: str, url: str = "", index: int | None = None) -> str:
        """模拟标签页操作。"""
        arguments: dict[str, Any] = {"action": action, "url": url}
        if index is not None:
            arguments["index"] = index
        return respond("browser_tabs", arguments)

    @tool("browser_fill_form")
    async def fill_form(fields: list[dict[str, Any]]) -> str:
        """模拟表单填写。"""
        return respond("browser_fill_form", {"fields": fields})

    @tool("browser_type")
    async def type_text(target: str, text: str) -> str:
        """模拟文本输入。"""
        return respond("browser_type", {"target": target, "text": text})

    @tool("browser_select_option")
    async def select_option(target: str, values: list[str]) -> str:
        """模拟下拉选择。"""
        return respond("browser_select_option", {"target": target, "values": values})

    @tool("browser_click")
    async def click(target: str) -> str:
        """模拟点击。"""
        return respond("browser_click", {"target": target})

    return [
        navigate,
        snapshot,
        find,
        wait_for,
        navigate_back,
        tabs,
        fill_form,
        type_text,
        select_option,
        click,
    ]


def test_browser_rejects_dangerous_or_private_urls_before_opening_session() -> None:
    """危险协议、本地主机和解析到私网的域名都不得启动浏览器。"""
    opened = 0

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[list[BaseTool]]:
        nonlocal opened
        opened += 1
        yield []

    async def private_resolver(_host: str, _port: int) -> list[str]:
        return ["10.0.0.8"]

    browser = import_module("tourism_agent.providers.browser")
    client = browser.PlaywrightBrowserClient(
        session_factory=session_factory,
        resolver=private_resolver,
    )

    async def scenario() -> None:
        for url in (
            "file:///etc/passwd",
            "http://localhost/admin",
            "https://internal.example/admin",
        ):
            with pytest.raises(ValueError):
                await client.invoke("thread-1", "browser_navigate", {"url": url})

    asyncio.run(scenario())

    assert opened == 0


def test_browser_accepts_proxy_fake_ip_only_for_domain_names() -> None:
    """兼容透明代理的 Fake-IP，但直接访问该保留地址仍应拒绝。"""
    calls: list[str] = []

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[list[BaseTool]]:
        yield fake_browser_tools(
            lambda name, _args: calls.append(name) or "Page URL: https://example.com"
        )

    async def fake_ip_resolver(_host: str, _port: int) -> list[str]:
        return ["198.18.0.173"]

    browser = import_module("tourism_agent.providers.browser")
    client = browser.PlaywrightBrowserClient(
        session_factory=session_factory,
        resolver=fake_ip_resolver,
    )

    async def scenario() -> None:
        await client.invoke(
            "thread-1",
            "browser_navigate",
            {"url": "https://example.com"},
        )
        with pytest.raises(ValueError, match="保留网络地址"):
            await client.invoke(
                "thread-1",
                "browser_navigate",
                {"url": "http://198.18.0.173"},
            )
        await client.close_all()

    asyncio.run(scenario())

    assert calls == ["browser_navigate"]


def test_playwright_session_does_not_start_custom_network_proxy(monkeypatch) -> None:
    """应用只做打开URL前校验，不应自行启动和维护网络出口代理。"""
    browser = import_module("tourism_agent.providers.browser")
    captured_config: dict[str, Any] = {}

    async def forbidden_start_server(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("不应启动本地出口代理")

    class FakeMcpClient:
        def __init__(self, config: dict[str, Any]) -> None:
            captured_config.update(config)

        @asynccontextmanager
        async def session(self, _name: str) -> AsyncIterator[object]:
            yield object()

    class FakeTemporaryDirectory:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> str:
            return "C:/temporary-playwright-output"

        def __exit__(self, *_args: object) -> None:
            return None

    async def fake_load_tools(_session: object) -> list[BaseTool]:
        return []

    monkeypatch.setattr(browser.asyncio, "start_server", forbidden_start_server)
    monkeypatch.setattr(browser, "MultiServerMCPClient", FakeMcpClient)
    monkeypatch.setattr(browser, "load_mcp_tools", fake_load_tools)
    monkeypatch.setattr(browser, "TemporaryDirectory", FakeTemporaryDirectory)

    async def scenario() -> None:
        async with browser.open_playwright_mcp_tools() as tools:
            assert tools == []

    asyncio.run(scenario())

    args = captured_config["playwright"]["args"]
    assert "--proxy-server" not in args


def test_browser_reuses_one_session_per_thread_and_closes_it_explicitly() -> None:
    """同一线程应复用页面状态，不同线程必须使用不同 MCP Session。"""
    opened: list[int] = []
    closed: list[int] = []

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[list[BaseTool]]:
        session_id = len(opened) + 1
        opened.append(session_id)

        try:
            yield fake_browser_tools(lambda _name, _args: f"session={session_id}")
        finally:
            closed.append(session_id)

    browser = import_module("tourism_agent.providers.browser")
    client = browser.PlaywrightBrowserClient(
        session_factory=session_factory,
        resolver=public_resolver,
    )

    async def scenario() -> tuple[str, str, str]:
        first = await client.invoke("thread-1", "browser_snapshot", {})
        second = await client.invoke("thread-1", "browser_snapshot", {})
        other = await client.invoke("thread-2", "browser_snapshot", {})
        await client.close_thread("thread-1")
        await client.close_all()
        return first, second, other

    first, second, other = asyncio.run(scenario())

    assert (first, second, other) == ("session=1", "session=1", "session=2")
    assert opened == [1, 2]
    assert closed == [1, 2]


def test_browser_enforces_action_and_tab_limits() -> None:
    """达到动作或标签页上限后必须在调用 MCP Tool 前停止。"""
    calls: list[dict[str, object]] = []

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[list[BaseTool]]:
        def respond(name: str, arguments: dict[str, Any]) -> str:
            if name == "browser_tabs":
                calls.append(arguments)
            return "tabs ok"

        yield fake_browser_tools(respond)

    browser = import_module("tourism_agent.providers.browser")
    client = browser.PlaywrightBrowserClient(
        session_factory=session_factory,
        resolver=public_resolver,
        max_actions=2,
        max_tabs=2,
    )

    async def scenario() -> None:
        await client.invoke(
            "thread-1",
            "browser_tabs",
            {"action": "new", "url": "https://example.com/one"},
        )
        with pytest.raises(ValueError, match="标签页"):
            await client.invoke(
                "thread-1",
                "browser_tabs",
                {"action": "new", "url": "https://example.com/two"},
            )
        await client.invoke("thread-1", "browser_tabs", {"action": "list"})
        with pytest.raises(ValueError, match="动作上限"):
            await client.invoke("thread-1", "browser_tabs", {"action": "list"})
        await client.close_all()

    asyncio.run(scenario())

    assert calls == [
        {"action": "new", "url": "https://example.com/one"},
        {"action": "list", "url": ""},
    ]


def test_browser_uses_snapshot_targets_to_reject_disguised_high_risk_clicks() -> None:
    """交互权限必须依据页面真实快照，而不是模型传入的 element 描述。"""
    click_targets: list[str] = []

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[list[BaseTool]]:
        def respond(name: str, arguments: dict[str, Any]) -> str:
            if name == "browser_snapshot":
                return (
                    "### Page\n- Page URL: https://example.com\n"
                    '- button "查询车次" [ref=e3]\n'
                    '- button "继续查看" [ref=e5]\n'
                    '- button "提交订单" [ref=e9]'
                )
            if name == "browser_click":
                click_targets.append(str(arguments["target"]))
            return "ok"

        yield fake_browser_tools(respond)

    browser = import_module("tourism_agent.providers.browser")
    client = browser.PlaywrightBrowserClient(
        session_factory=session_factory,
        resolver=public_resolver,
    )

    async def scenario() -> None:
        await client.invoke("thread-1", "browser_snapshot", {})
        with pytest.raises(ValueError, match="高风险"):
            await client.invoke(
                "thread-1",
                "browser_click",
                {"target": "e9", "element": "继续按钮"},
            )
        with pytest.raises(ValueError, match="快照"):
            await client.invoke(
                "thread-1",
                "browser_click",
                {"target": "e7", "element": "查询按钮"},
            )
        await client.invoke(
            "thread-1",
            "browser_click",
            {"target": "e3", "element": "查询按钮"},
        )
        await client.invoke("thread-1", "browser_snapshot", {})
        await client.invoke(
            "thread-1",
            "browser_click",
            {"target": "e5", "element": "继续查看"},
        )
        await client.close_all()

    asyncio.run(scenario())

    assert click_targets == ["e3", "e5"]


def test_browser_session_lifecycle_stays_in_one_owner_task() -> None:
    """MCP Session 的创建、调用和关闭必须发生在同一个所有者 Task。"""
    task_ids: dict[str, int] = {}

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[list[BaseTool]]:
        task_ids["enter"] = id(asyncio.current_task())

        def respond(_name: str, _arguments: dict[str, Any]) -> str:
            task_ids["invoke"] = id(asyncio.current_task())
            return "ok"

        try:
            yield fake_browser_tools(respond)
        finally:
            task_ids["exit"] = id(asyncio.current_task())

    browser = import_module("tourism_agent.providers.browser")
    client = browser.PlaywrightBrowserClient(
        session_factory=session_factory,
        resolver=public_resolver,
    )

    async def scenario() -> None:
        task_ids["api"] = id(asyncio.current_task())
        # 模拟 LangGraph ToolNode 在子 Task 调用，而 API 在父 Task 清理。
        await asyncio.create_task(client.invoke("thread-1", "browser_snapshot", {}))
        await client.close_thread("thread-1")

    asyncio.run(scenario())

    assert task_ids["enter"] == task_ids["exit"]
    assert task_ids["enter"] != task_ids["api"]


def test_empty_snapshot_replaces_previous_interactive_targets() -> None:
    """新快照没有 ref 时也必须清空旧目标，避免复用过期页面控件。"""
    snapshots = iter(
        [
            '- button "查询车次" [ref=e3]',
            "### Page\n- 页面当前没有可交互控件",
        ]
    )

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[list[BaseTool]]:
        def respond(name: str, _arguments: dict[str, Any]) -> str:
            return next(snapshots) if name == "browser_snapshot" else "ok"

        yield fake_browser_tools(respond)

    browser = import_module("tourism_agent.providers.browser")
    client = browser.PlaywrightBrowserClient(
        session_factory=session_factory,
        resolver=public_resolver,
    )

    async def scenario() -> None:
        await client.invoke("thread-1", "browser_snapshot", {})
        await client.invoke("thread-1", "browser_snapshot", {})
        with pytest.raises(ValueError, match="最近页面快照"):
            await client.invoke(
                "thread-1",
                "browser_click",
                {"target": "e3", "element": "查询车次"},
            )
        await client.close_all()

    asyncio.run(scenario())


def test_browser_session_start_has_lifecycle_timeout() -> None:
    """MCP 启动卡住时必须在有限时间内取消所有者 Task。"""
    never = asyncio.Event()

    @asynccontextmanager
    async def hanging_start() -> AsyncIterator[list[BaseTool]]:
        await never.wait()
        yield []

    browser = import_module("tourism_agent.providers.browser")

    async def scenario() -> None:
        start_client = browser.PlaywrightBrowserClient(
            session_factory=hanging_start,
            resolver=public_resolver,
            lifecycle_timeout_seconds=0.01,
        )
        with pytest.raises(ValueError, match="启动超时"):
            await start_client.invoke("thread-start", "browser_snapshot", {})

    asyncio.run(scenario())


def test_browser_session_close_has_lifecycle_timeout() -> None:
    """MCP 退出卡住时必须在有限时间内取消所有者 Task。"""
    release = asyncio.Event()
    owner_tasks: list[asyncio.Task[object]] = []

    @asynccontextmanager
    async def hanging_close() -> AsyncIterator[list[BaseTool]]:
        owner_tasks.append(asyncio.current_task())
        try:
            yield fake_browser_tools(lambda _name, _arguments: "ok")
        finally:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # 模拟供应商退出逻辑错误地吞掉取消。
                    continue

    browser = import_module("tourism_agent.providers.browser")

    async def scenario() -> None:
        close_client = browser.PlaywrightBrowserClient(
            session_factory=hanging_close,
            resolver=public_resolver,
            lifecycle_timeout_seconds=0.1,
        )
        await close_client.invoke("thread-close", "browser_snapshot", {})
        close_task = asyncio.create_task(close_client.close_thread("thread-close"))
        done, _pending = await asyncio.wait({close_task}, timeout=0.5)
        release.set()
        with pytest.raises(ValueError, match="关闭超时"):
            await close_task
        await owner_tasks[0]
        assert done == {close_task}

    asyncio.run(scenario())


def test_browser_closes_session_after_task_deadline() -> None:
    """浏览器任务超过总时限后应关闭会话，避免后台资源长期占用。"""
    now = 100.0
    closed = False

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[list[BaseTool]]:
        nonlocal closed
        try:
            yield fake_browser_tools(lambda _name, _args: "ok")
        finally:
            closed = True

    browser = import_module("tourism_agent.providers.browser")
    client = browser.PlaywrightBrowserClient(
        session_factory=session_factory,
        resolver=public_resolver,
        max_task_seconds=90,
        clock=lambda: now,
    )

    async def scenario() -> None:
        nonlocal now
        await client.invoke("thread-1", "browser_snapshot", {})
        now = 191.0
        with pytest.raises(ValueError, match="总时限"):
            await client.invoke("thread-1", "browser_snapshot", {})

    asyncio.run(scenario())

    assert closed is True


def test_browser_interrupts_action_when_remaining_task_time_expires() -> None:
    """动作已经开始后仍必须受任务剩余时间约束。"""
    closed = False

    @tool("browser_snapshot")
    async def slow_snapshot() -> str:
        """模拟卡住的 MCP 动作。"""
        await asyncio.sleep(0.05)
        return "too late"

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[list[BaseTool]]:
        nonlocal closed
        tools = fake_browser_tools(lambda _name, _args: "ok")
        tools[1] = slow_snapshot
        try:
            yield tools
        finally:
            closed = True

    browser = import_module("tourism_agent.providers.browser")
    client = browser.PlaywrightBrowserClient(
        session_factory=session_factory,
        resolver=public_resolver,
        max_task_seconds=0.01,
    )

    async def scenario() -> None:
        with pytest.raises(ValueError, match="总时限"):
            await client.invoke("thread-1", "browser_snapshot", {})

    asyncio.run(scenario())

    assert closed is True
