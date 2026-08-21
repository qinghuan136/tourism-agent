"""验证 Helper 可绑定的显式浏览器 Tool 包装。"""

import asyncio
from importlib import import_module
from typing import Any

import pytest
from langgraph.prebuilt import ToolRuntime


class FakeBrowserClient:
    """记录包装 Tool 传给浏览器客户端的线程和参数。"""

    def __init__(self) -> None:
        self.call: tuple[str, str, dict[str, Any]] | None = None

    async def invoke(
        self,
        thread_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        self.call = (thread_id, tool_name, arguments)
        return "Page URL: https://example.com/result\n公开查询结果"


def tool_runtime(thread_id: str) -> ToolRuntime:
    """构造包含 thread_id 的最小 LangGraph ToolRuntime。"""
    return ToolRuntime(
        state={},
        context=None,
        config={"configurable": {"thread_id": thread_id}},
        stream_writer=lambda _chunk: None,
        tool_call_id="browser-call-1",
        store=None,
    )


def test_browser_tools_expose_only_approved_actions_and_forward_thread_id() -> None:
    """公共包装只应暴露十个获准动作，并把 thread_id 交给隔离客户端。"""
    browser_tools = import_module("tourism_agent.graph.tools.browser")
    client = FakeBrowserClient()
    tools = browser_tools.create_browser_tools(client)
    tools_by_name = {item.name: item for item in tools}

    result = asyncio.run(
        tools_by_name["browser_navigate"].coroutine(
            "https://example.com/search",
            tool_runtime("trip-thread-1"),
        )
    )

    assert list(tools_by_name) == [
        "browser_navigate",
        "browser_snapshot",
        "browser_find",
        "browser_wait_for",
        "browser_navigate_back",
        "browser_tabs",
        "browser_fill_form",
        "browser_type",
        "browser_select_option",
        "browser_click",
    ]
    assert client.call == (
        "trip-thread-1",
        "browser_navigate",
        {"url": "https://example.com/search"},
    )
    assert result.startswith("[不可信外部数据：")
    assert result.endswith("\n公开查询结果")


def test_browser_tools_reject_high_risk_interactions_before_client_call() -> None:
    """登录、验证码和交易控件不得进入真实浏览器 Client。"""
    browser_tools = import_module("tourism_agent.graph.tools.browser")
    client = FakeBrowserClient()
    tools = {item.name: item for item in browser_tools.create_browser_tools(client)}
    runtime = tool_runtime("trip-thread-1")

    async def scenario() -> None:
        with pytest.raises(ValueError, match="高风险"):
            await tools["browser_click"].coroutine(
                "button-ref",
                runtime,
                element="提交订单按钮",
            )
        with pytest.raises(ValueError, match="高风险"):
            await tools["browser_type"].coroutine(
                "input-ref",
                "secret",
                runtime,
                element="账号密码输入框",
            )
        with pytest.raises(ValueError, match="高风险"):
            await tools["browser_fill_form"].coroutine(
                [{"target": "card-ref", "name": "银行卡号", "type": "textbox", "value": "1"}],
                runtime,
            )

    asyncio.run(scenario())

    assert client.call is None
