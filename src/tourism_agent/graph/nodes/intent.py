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
- planning：用户要生成、调整或确认具体行程安排，重点是形成可执行的旅行方案；
- explore：用户要发现、推荐或比较目的地、地点、活动和旅行风格；即使已经知道城市，只要主要诉求
  仍是开放式寻找候选项，也应选择 explore；
- research：用户要围绕相对明确的旅行对象或问题进行深入调查、核实来源、分析风险或形成有依据的
  研究结论；重点是“明确对象实际怎么样”，而不是广泛寻找候选项或编排完整行程；
- helper：默认兜底模块。用户要进行轻量对话、解释已有信息、查询局部公开事实或完成简单比较时选择；
  不能明确归入 planning、explore、research 的请求也默认选择 helper。订单、预订、支付、退款等副作用，
  以及危险、不合法或明显超出能力的请求，同样交给 helper，由它根据实际 Tool 能力拒绝或提供替代帮助。

不要根据你对系统能力的猜测拒绝请求。你只判断最合适的处理模块，不判断请求最终能否完成。
查询车次、票价等只读信息属于 helper；要求购票、下单或支付也交给 helper，由它负责说明能力边界。

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
