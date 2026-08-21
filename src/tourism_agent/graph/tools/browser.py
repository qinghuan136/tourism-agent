"""把受限 Playwright Client 包装成 Helper 可绑定的显式 Tools。"""

import logging
import time
from typing import Annotated, Any, Literal, NotRequired, Protocol, TypedDict

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from tourism_agent.graph.tools.travel_query import mark_untrusted_external_data
from tourism_agent.infrastructure.logging_config import log_preview

logger = logging.getLogger(__name__)
HIGH_RISK_BROWSER_LABELS = (
    "登录",
    "注册",
    "账号",
    "密码",
    "验证码",
    "银行卡",
    "身份证",
    "提交订单",
    "确认订单",
    "立即购买",
    "立即预订",
    "支付",
    "付款",
    "退款",
    "取消订单",
    "改签",
    "上传",
    "下载",
    "sign in",
    "log in",
    "password",
    "verification code",
    "checkout",
    "place order",
    "purchase",
    "payment",
    "refund",
    "upload",
    "download",
)


class BrowserFormField(TypedDict):
    """描述一个获准填写的非敏感公开查询字段。"""

    target: Annotated[str, "必须复制最近一次 browser_snapshot 返回的 e数字 ref"]
    name: str
    type: Literal["textbox", "checkbox", "radio", "combobox", "slider"]
    value: str
    element: NotRequired[str]


class BrowserClient(Protocol):
    """浏览器 Tool 依赖的最小客户端接口。"""

    async def invoke(
        self,
        thread_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str: ...


def create_browser_tools(browser_client: BrowserClient) -> list[BaseTool]:
    """创建仅包含公开网页低风险动作的 Playwright Tool 白名单。"""

    async def invoke(
        name: str,
        arguments: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        thread_id = str(runtime.config.get("configurable", {}).get("thread_id", ""))
        if not thread_id:
            raise ValueError("浏览器 Tool 运行配置缺少 thread_id")
        logger.info(
            "Tool调用开始 name=%s thread_id=%s args=%s",
            name,
            thread_id,
            log_preview(arguments),
        )
        started_at = time.perf_counter()
        try:
            result = await browser_client.invoke(thread_id, name, arguments)
        except Exception:
            logger.exception("Tool调用失败 name=%s thread_id=%s", name, thread_id)
            raise
        logger.info(
            "Tool调用完成 name=%s thread_id=%s elapsed_ms=%d result=%s",
            name,
            thread_id,
            round((time.perf_counter() - started_at) * 1000),
            log_preview(result),
        )
        return mark_untrusted_external_data(result)

    @tool
    async def browser_navigate(url: str, runtime: ToolRuntime) -> str:
        """打开一个已通过安全校验的匿名公开 HTTP/HTTPS 网页。"""
        return await invoke("browser_navigate", {"url": url}, runtime)

    @tool
    async def browser_snapshot(
        runtime: ToolRuntime,
        target: str = "",
        depth: int | None = None,
    ) -> str:
        """读取当前公开页面或目标元素的结构化可访问性快照。"""
        arguments: dict[str, Any] = {}
        if target:
            arguments["target"] = target
        if depth is not None:
            arguments["depth"] = depth
        return await invoke("browser_snapshot", arguments, runtime)

    @tool
    async def browser_find(
        runtime: ToolRuntime,
        text: str = "",
        regex: str = "",
    ) -> str:
        """在当前页面快照中按普通文本或正则定位相关元素。"""
        if bool(text) == bool(regex):
            raise ValueError("browser_find 必须且只能提供 text 或 regex 其中一个")
        arguments = {"text": text} if text else {"regex": regex}
        return await invoke("browser_find", arguments, runtime)

    @tool
    async def browser_wait_for(
        runtime: ToolRuntime,
        seconds: float | None = None,
        text: str = "",
        text_gone: str = "",
    ) -> str:
        """等待动态页面出现文本、文本消失或经过少量秒数。"""
        arguments: dict[str, Any] = {}
        if seconds is not None:
            arguments["time"] = seconds
        if text:
            arguments["text"] = text
        if text_gone:
            arguments["textGone"] = text_gone
        return await invoke("browser_wait_for", arguments, runtime)

    @tool
    async def browser_navigate_back(runtime: ToolRuntime) -> str:
        """返回本次匿名浏览会话中的上一公开页面。"""
        return await invoke("browser_navigate_back", {}, runtime)

    @tool
    async def browser_tabs(
        action: Literal["list", "new", "close", "select"],
        runtime: ToolRuntime,
        index: int | None = None,
        url: str = "",
    ) -> str:
        """列出、创建、关闭或切换本次匿名会话内的有限标签页。"""
        arguments: dict[str, Any] = {"action": action}
        if index is not None:
            arguments["index"] = index
        if url:
            arguments["url"] = url
        return await invoke("browser_tabs", arguments, runtime)

    @tool
    async def browser_fill_form(
        fields: list[BrowserFormField],
        runtime: ToolRuntime,
    ) -> str:
        """填写公开查询页中的地点、日期、人数或筛选条件，不得填写敏感信息。"""
        for field in fields:
            ensure_safe_browser_interaction(field["name"], field.get("element", ""))
        return await invoke("browser_fill_form", {"fields": fields}, runtime)

    @tool
    async def browser_type(
        target: Annotated[str, "必须复制最近一次 browser_snapshot 返回的 e数字 ref"],
        text: str,
        runtime: ToolRuntime,
        element: str,
        submit: bool = False,
        slowly: bool = False,
    ) -> str:
        """向最近页面快照中指定 ref 的公开查询控件输入非敏感文本。"""
        ensure_safe_browser_interaction(element)
        arguments: dict[str, Any] = {"target": target, "text": text}
        arguments["element"] = element
        if submit:
            arguments["submit"] = True
        if slowly:
            arguments["slowly"] = True
        return await invoke("browser_type", arguments, runtime)

    @tool
    async def browser_select_option(
        target: Annotated[str, "必须复制最近一次 browser_snapshot 返回的 e数字 ref"],
        values: list[str],
        runtime: ToolRuntime,
        element: str,
    ) -> str:
        """选择公开查询页面中的一个或多个下拉筛选项。"""
        ensure_safe_browser_interaction(element)
        arguments: dict[str, Any] = {"target": target, "values": values}
        arguments["element"] = element
        return await invoke("browser_select_option", arguments, runtime)

    @tool
    async def browser_click(
        target: Annotated[str, "必须复制最近一次 browser_snapshot 返回的 e数字 ref"],
        runtime: ToolRuntime,
        element: str,
        double_click: bool = False,
        button: Literal["left", "right", "middle"] = "left",
        modifiers: list[Literal["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]]
        | None = None,
    ) -> str:
        """点击公开网页中的搜索、筛选、分页或展开详情控件。"""
        ensure_safe_browser_interaction(element)
        arguments: dict[str, Any] = {"target": target}
        arguments["element"] = element
        if double_click:
            arguments["doubleClick"] = True
        if button != "left":
            arguments["button"] = button
        if modifiers:
            arguments["modifiers"] = modifiers
        return await invoke("browser_click", arguments, runtime)

    return [
        browser_navigate,
        browser_snapshot,
        browser_find,
        browser_wait_for,
        browser_navigate_back,
        browser_tabs,
        browser_fill_form,
        browser_type,
        browser_select_option,
        browser_click,
    ]


def ensure_safe_browser_interaction(*labels: str) -> None:
    """拒绝明显涉及身份、敏感数据、交易或文件的交互控件。"""
    normalized = " ".join(labels).casefold()
    if any(keyword in normalized for keyword in HIGH_RISK_BROWSER_LABELS):
        raise ValueError("浏览器拒绝登录、敏感信息、订单或其他高风险网页操作")
