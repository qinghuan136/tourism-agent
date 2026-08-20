"""验证理解节点只根据用户语义返回受约束的路由。"""

import asyncio
import logging
from importlib import import_module
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableLambda

from tourism_agent.models.contracts import IntentDecision, RouteTarget


class SemanticFakeModel:
    """用本地语义规则代替外部 LLM，验证节点传入的真实消息。"""

    def with_structured_output(self, schema: type[IntentDecision]) -> RunnableLambda:
        def decide(messages: list[BaseMessage]) -> Any:
            user_input = str(messages[-1].content)
            if "规划" in user_input:
                route = "planning"
            elif "灵感" in user_input or "去哪" in user_input:
                route = "inspiration"
            else:
                route = "unsupported"
            return schema(route=route)

        return RunnableLambda(decide)


@pytest.mark.parametrize(
    ("user_input", "expected_route"),
    [
        ("帮我规划北京三日游", RouteTarget.PLANNING),
        ("不知道去哪儿，给我一些旅行灵感", RouteTarget.INSPIRATION),
        ("帮我写一段排序代码", RouteTarget.UNSUPPORTED),
    ],
)
def test_intent_node_returns_route_without_business_response(
    user_input: str,
    expected_route: RouteTarget,
    caplog,
) -> None:
    """理解节点只更新 route，并把业务处理留给后续子图。"""
    intent_module = import_module("tourism_agent.graph.nodes.intent")
    intent_node = intent_module.create_intent_node(SemanticFakeModel())

    with caplog.at_level(logging.INFO, logger="tourism_agent.graph.nodes.intent"):
        result = asyncio.run(intent_node({"user_input": user_input}))

    assert result == {"route": expected_route}
    assert "理解节点进入" in caplog.text
    assert f"理解节点完成 route={expected_route.value}" in caplog.text
