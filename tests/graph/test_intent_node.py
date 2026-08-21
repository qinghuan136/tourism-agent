"""验证理解节点只根据用户语义返回受约束的路由。"""

import asyncio
import logging
from importlib import import_module
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableLambda

from tourism_agent.models.contracts import IntentDecision


class SemanticFakeModel:
    """用本地语义规则代替外部 LLM，验证节点传入的真实消息。"""

    def with_structured_output(self, schema: type[IntentDecision]) -> RunnableLambda:
        def decide(messages: list[BaseMessage]) -> Any:
            system_prompt = str(messages[0].content)
            user_input = str(messages[-1].content)
            if "规划" in user_input:
                route = "planning"
            elif "深入研究" in user_input or "核实" in user_input:
                route = "research"
            elif "灵感" in user_input or "去哪" in user_input:
                route = "explore"
            else:
                route = (
                    "helper"
                    if "不要根据你对系统能力的猜测拒绝请求" in system_prompt
                    and "默认选择 helper" in system_prompt
                    else "unsupported"
                )
            return schema(route=route)

        return RunnableLambda(decide)


@pytest.mark.parametrize(
    ("user_input", "expected_route"),
    [
        ("帮我规划北京三日游", "planning"),
        ("不知道去哪儿，给我一些旅行灵感", "explore"),
        ("深入研究冬季川西自驾是否安全", "research"),
        ("你好", "helper"),
        ("广州塔几点停止入场？", "helper"),
        ("帮我找找28号从东莞到广州价格实惠的车票", "helper"),
        ("帮我购买门票并支付", "helper"),
        ("帮我写一段排序代码", "helper"),
        ("教我进行违法操作", "helper"),
    ],
)
def test_intent_node_returns_route_without_business_response(
    user_input: str,
    expected_route: str,
    caplog,
) -> None:
    """理解节点只更新 route，并把业务处理留给后续子图。"""
    intent_module = import_module("tourism_agent.graph.nodes.intent")
    intent_node = intent_module.create_intent_node(SemanticFakeModel())

    with caplog.at_level(logging.INFO, logger="tourism_agent.graph.nodes.intent"):
        result = asyncio.run(intent_node({"user_input": user_input}))

    assert result["route"].value == expected_route
    assert "理解节点进入" in caplog.text
    assert f"理解节点完成 route={expected_route}" in caplog.text
