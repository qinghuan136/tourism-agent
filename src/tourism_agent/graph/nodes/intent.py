"""实现根图中只负责路由判断的理解节点。"""

import logging
from collections.abc import Awaitable, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from tourism_agent.graph.messages import conversation_to_messages
from tourism_agent.graph.state import RootState
from tourism_agent.infrastructure.logging_config import log_preview
from tourism_agent.models.contracts import IntentDecision, RouteTarget

INTENT_SYSTEM_PROMPT = """
你是旅行 Agent 根图中的理解 Agent，只负责为用户请求选择处理模块。

可选路由：
- planning：用户已经有旅行目标，需要生成、调整或查询具体旅行方案；
- inspiration：用户尚未确定目的地或方向，希望获得旅行灵感和推荐；
- unsupported：请求不属于当前支持的旅行能力。

不要回答用户问题，不要生成旅行方案，也不要调用任何 Tool。
只按照 IntentDecision 的结构返回一个路由。

输入消息被明确分成两类：
- 带有【历史消息】标签的消息是此前 Conversation，只用于理解指代、省略和上下文承接；
- 带有【当前消息】标签的最后一条 HumanMessage，才是本次需要路由的用户请求。

必须只为【当前消息】选择路由。不要把历史消息当成本轮新请求，也不要延续历史 Assistant
消息中的业务回答。
""".strip()

IntentNode = Callable[[RootState], Awaitable[dict[str, RouteTarget]]]
logger = logging.getLogger(__name__)


def create_intent_node(model: BaseChatModel) -> IntentNode:
    """为指定模型创建只返回结构化路由结果的理解节点。"""
    structured_model = model.with_structured_output(IntentDecision)

    async def understand_intent(state: RootState) -> dict[str, RouteTarget]:
        logger.info("理解节点进入 user_input=%s", log_preview(state["user_input"]))
        history = conversation_to_messages(
            state.get("routing_context", []),
            label="【历史消息】",
        )
        decision = await structured_model.ainvoke(
            [
                SystemMessage(content=INTENT_SYSTEM_PROMPT),
                *history,
                HumanMessage(content=f"【当前消息】\n{state['user_input']}"),
            ]
        )
        logger.info("理解节点完成 route=%s", decision.route.value)
        return {"route": decision.route}

    return understand_intent
