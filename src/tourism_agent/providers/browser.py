"""管理按 thread_id 隔离的匿名 Playwright MCP 会话。"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from tourism_agent.infrastructure.logging_config import log_preview

logger = logging.getLogger(__name__)
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.79"
PLAYWRIGHT_TOOL_NAMES = (
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
)
INTERACTIVE_BROWSER_TOOLS = {
    "browser_fill_form",
    "browser_type",
    "browser_select_option",
    "browser_click",
}
PAGE_STATE_CHANGING_TOOLS = INTERACTIVE_BROWSER_TOOLS | {
    "browser_navigate",
    "browser_navigate_back",
    "browser_tabs",
    "browser_wait_for",
}
HIGH_RISK_PAGE_LABELS = (
    "登录", "注册", "账号", "密码", "验证码", "银行卡", "身份证",
    "提交订单", "确认订单", "购买", "预订", "预约", "支付", "付款",
    "退款", "取消订单", "改签", "发送", "上传", "下载",
    "sign in", "log in", "password", "verification code", "checkout",
    "place order", "purchase", "book now", "reserve", "payment", "refund",
    "send", "upload", "download",
)
MAX_BROWSER_RESULT_CHARS = 20_000
PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
DEFAULT_PLAYWRIGHT_BROWSER = "msedge" if sys.platform == "win32" else "chromium"
BLOCKED_ORIGINS = (
    "http://localhost:*;https://localhost:*;"
    "http://127.0.0.1:*;https://127.0.0.1:*;"
    "http://[::1]:*;https://[::1]:*;"
    "http://10.*;https://10.*;"
    "http://172.16.*;https://172.16.*;"
    "http://172.17.*;https://172.17.*;"
    "http://172.18.*;https://172.18.*;"
    "http://172.19.*;https://172.19.*;"
    "http://192.168.*;https://192.168.*;"
    "http://169.254.169.254:*;https://169.254.169.254:*"
)

HostResolver = Callable[[str, int], Awaitable[list[str]]]
SessionFactory = Callable[[], AbstractAsyncContextManager[Sequence[BaseTool]]]


@dataclass
class BrowserRuntimeSession:
    """只在会话所有者 Task 内访问的 MCP 工具和资源计数。"""

    tools: dict[str, BaseTool]
    created_at: float
    action_count: int = 0
    tab_count: int = 1
    interactive_targets: dict[str, str] = field(default_factory=dict)


@dataclass
class BrowserCommand:
    """把调用请求转交给会话所有者 Task。"""

    tool_name: str
    arguments: dict[str, Any]
    result: asyncio.Future[str]


@dataclass
class BrowserThreadSession:
    """保存 thread 对应的所有者 Task 和串行命令队列。"""

    commands: asyncio.Queue[BrowserCommand | None]
    owner_task: asyncio.Task[None]
    ready: asyncio.Future[None]


class PlaywrightBrowserClient:
    """通过受限匿名 Playwright 调用公开网页，并隔离不同会话。"""

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        resolver: HostResolver | None = None,
        max_actions: int = 12,
        max_tabs: int = 3,
        max_task_seconds: float = 90,
        lifecycle_timeout_seconds: float = 15,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._session_factory = session_factory or open_playwright_mcp_tools
        self._resolver = resolver or resolve_host_addresses
        self._max_actions = max_actions
        self._max_tabs = max_tabs
        self._max_task_seconds = max_task_seconds
        self._lifecycle_timeout_seconds = lifecycle_timeout_seconds
        self._clock = clock or time.monotonic
        self._sessions: dict[str, BrowserThreadSession] = {}
        self._sessions_lock = asyncio.Lock()

    async def invoke(
        self,
        thread_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """校验并串行执行一个浏览器动作。"""
        if tool_name not in PLAYWRIGHT_TOOL_NAMES:
            raise ValueError(f"浏览器 Tool 不在允许白名单中：{tool_name}")
        await self._validate_action_url(tool_name, arguments)
        session = await self._get_session(thread_id)
        result = asyncio.get_running_loop().create_future()
        await session.commands.put(
            BrowserCommand(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
            )
        )
        try:
            return await result
        except BrowserTaskTimeoutError:
            await self.close_thread(thread_id)
            raise

    async def close_thread(self, thread_id: str) -> None:
        """关闭一个 thread 的页面、匿名上下文和 MCP 子进程。"""
        async with self._sessions_lock:
            session = self._sessions.pop(thread_id, None)
        if session is None:
            return
        await session.commands.put(None)
        try:
            async with asyncio.timeout(self._lifecycle_timeout_seconds):
                await asyncio.shield(session.owner_task)
        except TimeoutError as error:
            await self._cancel_owner_task(session.owner_task)
            raise BrowserTaskTimeoutError("Playwright MCP 会话关闭超时") from error
        except asyncio.CancelledError:
            await self._cancel_owner_task(session.owner_task)
            raise
        logger.info("Playwright线程会话已关闭 thread_id=%s", thread_id)

    async def close_all(self) -> None:
        """关闭应用进程内仍然存活的全部浏览器会话。"""
        async with self._sessions_lock:
            thread_ids = list(self._sessions)
        for thread_id in thread_ids:
            await self.close_thread(thread_id)

    async def _get_session(self, thread_id: str) -> BrowserThreadSession:
        """懒创建并复用当前 thread 的隔离 MCP Session 所有者。"""
        async with self._sessions_lock:
            existing = self._sessions.get(thread_id)
            if existing is not None:
                session = existing
            else:
                loop = asyncio.get_running_loop()
                commands: asyncio.Queue[BrowserCommand | None] = asyncio.Queue()
                ready = loop.create_future()
                owner_task = asyncio.create_task(
                    self._run_session_owner(thread_id, commands, ready),
                    name=f"playwright-session-{thread_id}",
                )
                session = BrowserThreadSession(commands, owner_task, ready)
                self._sessions[thread_id] = session
        try:
            async with asyncio.timeout(self._lifecycle_timeout_seconds):
                await asyncio.shield(session.ready)
        except TimeoutError as error:
            async with self._sessions_lock:
                if self._sessions.get(thread_id) is session:
                    self._sessions.pop(thread_id)
            await self._cancel_owner_task(session.owner_task)
            raise BrowserTaskTimeoutError("Playwright MCP 会话启动超时") from error
        except asyncio.CancelledError:
            async with self._sessions_lock:
                if self._sessions.get(thread_id) is session:
                    self._sessions.pop(thread_id)
            await self._cancel_owner_task(session.owner_task)
            raise
        except Exception:
            async with self._sessions_lock:
                if self._sessions.get(thread_id) is session:
                    self._sessions.pop(thread_id)
            await session.owner_task
            raise
        return session

    async def _cancel_owner_task(self, owner_task: asyncio.Task[None]) -> None:
        """取消所有者，并在退出清理由首次取消触发时补发一次取消。"""
        owner_task.cancel()
        try:
            async with asyncio.timeout(self._lifecycle_timeout_seconds):
                await asyncio.shield(owner_task)
        except TimeoutError:
            # 首次取消可能正用于进入 Session 的 finally；再次取消中断卡住的异步退出。
            owner_task.cancel()
            with suppress(asyncio.CancelledError):
                await owner_task
        except asyncio.CancelledError:
            pass

    async def _run_session_owner(
        self,
        thread_id: str,
        commands: asyncio.Queue[BrowserCommand | None],
        ready: asyncio.Future[None],
    ) -> None:
        """在固定 Task 中创建、使用并关闭一个 MCP Session。"""
        try:
            async with self._session_factory() as loaded_tools:
                tools = {tool.name: tool for tool in loaded_tools}
                missing = [name for name in PLAYWRIGHT_TOOL_NAMES if name not in tools]
                if missing:
                    raise RuntimeError(
                        "Playwright MCP 缺少必需 Tools：" + "、".join(missing)
                    )
                runtime = BrowserRuntimeSession(
                    tools=tools,
                    created_at=self._clock(),
                )
                ready.set_result(None)
                logger.info("Playwright线程会话已创建 thread_id=%s", thread_id)
                while True:
                    command = await commands.get()
                    if command is None:
                        break
                    try:
                        value = await self._execute_action(
                            thread_id,
                            runtime,
                            command.tool_name,
                            command.arguments,
                        )
                    # 所有者 Task 不能因单次 Tool 异常退出；原异常会原样交还调用方。
                    except Exception as error:  # noqa: BLE001
                        if not command.result.done():
                            command.result.set_exception(error)
                    else:
                        if not command.result.done():
                            command.result.set_result(value)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not ready.done():
                ready.set_exception(error)
            else:
                logger.exception("Playwright会话所有者异常 thread_id=%s", thread_id)

    async def _execute_action(
        self,
        thread_id: str,
        session: BrowserRuntimeSession,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """在会话所有者 Task 内校验并执行一次浏览器动作。"""
        if self._clock() - session.created_at >= self._max_task_seconds:
            raise BrowserTaskTimeoutError(
                f"本次浏览器任务已达到{self._max_task_seconds:g}秒总时限"
            )
        validate_browser_interaction(session, tool_name, arguments)
        if session.action_count >= self._max_actions:
            raise ValueError(f"本次浏览器任务已达到{self._max_actions}次动作上限")
        if (
            tool_name == "browser_tabs"
            and arguments.get("action") == "new"
            and session.tab_count >= self._max_tabs
        ):
            raise ValueError(f"本次浏览器任务最多允许打开{self._max_tabs}个标签页")
        session.action_count += 1
        logger.info(
            "Playwright调用开始 thread_id=%s name=%s action=%d/%d args=%s",
            thread_id,
            tool_name,
            session.action_count,
            self._max_actions,
            log_preview(arguments),
        )
        remaining_seconds = self._max_task_seconds - (
            self._clock() - session.created_at
        )
        try:
            async with asyncio.timeout(remaining_seconds):
                raw_result = await session.tools[tool_name].ainvoke(arguments)
        except TimeoutError as error:
            raise BrowserTaskTimeoutError(
                f"本次浏览器任务已达到{self._max_task_seconds:g}秒总时限"
            ) from error
        result = normalize_mcp_result(raw_result)
        update_snapshot_targets(session, tool_name, result)
        if tool_name == "browser_tabs" and arguments.get("action") == "new":
            session.tab_count += 1
        elif tool_name == "browser_tabs" and arguments.get("action") == "close":
            session.tab_count = max(1, session.tab_count - 1)
        logger.info(
            "Playwright调用完成 thread_id=%s name=%s result=%s",
            thread_id,
            tool_name,
            log_preview(result),
        )
        return result

    async def _validate_action_url(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        """在浏览器连接目标前校验所有可直接携带 URL 的动作。"""
        url = ""
        if tool_name == "browser_navigate" or (
            tool_name == "browser_tabs" and arguments.get("action") == "new"
        ):
            url = str(arguments.get("url", ""))
        if url:
            await validate_public_url(url, self._resolver)

class BrowserTaskTimeoutError(ValueError):
    """表示浏览器任务已经用完当前 thread 的总时间预算。"""


@asynccontextmanager
async def open_playwright_mcp_tools() -> AsyncIterator[list[BaseTool]]:
    """为单个 thread 打开独立、匿名、无持久化状态的 MCP Server。"""
    with TemporaryDirectory(prefix="tourism-playwright-") as output_dir:
        client = MultiServerMCPClient(
            {
                "playwright": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": [
                        "-y",
                        PLAYWRIGHT_MCP_PACKAGE,
                        "--headless",
                        "--isolated",
                        "--browser",
                        DEFAULT_PLAYWRIGHT_BROWSER,
                        "--block-service-workers",
                        "--image-responses",
                        "omit",
                        "--output-dir",
                        output_dir,
                        "--timeout-navigation",
                        "30000",
                        "--timeout-action",
                        "5000",
                        "--blocked-origins",
                        BLOCKED_ORIGINS,
                    ],
                }
            }
        )
        async with client.session("playwright") as session:
            tools = await load_mcp_tools(session)
            yield tools


async def validate_public_url(url: str, resolver: HostResolver) -> None:
    """只允许无凭据的公开 HTTP/HTTPS 地址，并阻止私网解析。"""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("浏览器只允许访问公开 HTTP/HTTPS 网页")
    if parsed.username or parsed.password:
        raise ValueError("浏览器 URL 不得包含账号或密码")
    host = parsed.hostname
    if not host or host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise ValueError("浏览器不得访问本地主机")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("浏览器 URL 端口无效") from error
    await resolve_public_target(host, port, resolver)


async def resolve_public_target(
    host: str,
    port: int,
    resolver: HostResolver,
) -> str:
    """解析并返回一个已通过公网边界检查的连接地址。"""
    try:
        addresses = [str(ipaddress.ip_address(host))]
        host_is_ip_literal = True
    except ValueError:
        host_is_ip_literal = False
        addresses = await resolver(host, port)
    if not addresses:
        raise ValueError("浏览器目标域名没有可用地址")
    parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
    if not host_is_ip_literal and all(
        address in PROXY_FAKE_IP_NETWORK for address in parsed_addresses
    ):
        logger.info("浏览器域名使用代理Fake-IP host=%s", host)
        return addresses[0]
    if any(not address.is_global for address in parsed_addresses):
        raise ValueError("浏览器不得访问本地、私有或保留网络地址")
    return addresses[0]


async def resolve_host_addresses(host: str, port: int) -> list[str]:
    """解析目标域名的全部地址，供公开网络边界检查。"""
    records = await asyncio.to_thread(
        socket.getaddrinfo,
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    return list({record[4][0] for record in records})


def normalize_mcp_result(raw_result: Any) -> str:
    """将 MCP 文本块或结构化结果转成受长度限制的 Observation。"""
    if isinstance(raw_result, str):
        text = raw_result
    elif isinstance(raw_result, list):
        parts = [
            str(block.get("text"))
            for block in raw_result
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        text = "\n".join(parts) if parts else json.dumps(raw_result, ensure_ascii=False)
    else:
        text = json.dumps(raw_result, ensure_ascii=False)
    if len(text) <= MAX_BROWSER_RESULT_CHARS:
        return text
    return text[:MAX_BROWSER_RESULT_CHARS] + "\n[浏览器结果已按长度上限截断]"


def validate_browser_interaction(
    session: BrowserRuntimeSession,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """根据真实页面快照限制交互目标，不信任模型提供的控件描述。"""
    if tool_name not in INTERACTIVE_BROWSER_TOOLS:
        return
    if tool_name == "browser_type" and arguments.get("submit"):
        raise ValueError("浏览器输入 Tool 不允许直接提交表单")
    targets = (
        [str(field.get("target", "")) for field in arguments.get("fields", [])]
        if tool_name == "browser_fill_form"
        else [str(arguments.get("target", ""))]
    )
    for target in targets:
        if not re.fullmatch(r"e\d+", target):
            raise ValueError("浏览器交互必须使用最近页面快照中的 ref")
        actual_label = session.interactive_targets.get(target)
        if actual_label is None:
            raise ValueError("浏览器交互目标不存在于最近页面快照中")
        normalized = actual_label.casefold()
        if any(keyword in normalized for keyword in HIGH_RISK_PAGE_LABELS):
            raise ValueError("浏览器拒绝页面快照中的高风险交互目标")


def update_snapshot_targets(
    session: BrowserRuntimeSession,
    tool_name: str,
    result: str,
) -> None:
    """从 MCP 返回的真实快照建立下一次交互可用的 ref 白名单。"""
    targets: dict[str, str] = {}
    for line in result.splitlines():
        match = re.search(r"\[ref=(e\d+)\]", line)
        if match:
            targets[match.group(1)] = line.strip()
    if tool_name == "browser_snapshot" or targets:
        session.interactive_targets = targets
    elif tool_name in PAGE_STATE_CHANGING_TOOLS:
        session.interactive_targets.clear()
