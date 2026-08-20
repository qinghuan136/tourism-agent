"""提供默认关闭、需要显式启用的真实模型 Smoke Test。"""

import asyncio

import pytest

from tourism_agent.graph.nodes.intent import create_intent_node
from tourism_agent.models.contracts import RouteTarget
from tourism_agent.providers.model import ModelSettings, create_chat_model

settings = ModelSettings()

pytestmark = pytest.mark.skipif(
    not settings.run_llm_integration,
    reason="仅在 RUN_LLM_INTEGRATION=true 时调用真实模型",
)


def test_real_model_returns_structured_planning_route() -> None:
    """用一次最小调用验证模型连接和根图结构化路由能力。"""
    intent_node = create_intent_node(create_chat_model(settings))

    result = asyncio.run(intent_node({"user_input": "帮我规划北京三日游"}))

    assert result == {"route": RouteTarget.PLANNING}
