"""提供默认关闭、需要显式启用的真实模型 Smoke Test。"""

import asyncio

import pytest

from tourism_agent.graph.nodes.orchestrator import create_plan_node
from tourism_agent.models.orchestration import TaskType
from tourism_agent.providers.model import ModelSettings, create_chat_model

settings = ModelSettings()

pytestmark = pytest.mark.skipif(
    not settings.run_llm_integration,
    reason="仅在 RUN_LLM_INTEGRATION=true 时调用真实模型",
)


def test_real_model_returns_structured_orchestration_plan() -> None:
    """用一次最小调用验证模型连接和根图结构化任务计划能力。"""
    node = create_plan_node(create_chat_model(settings))

    result = asyncio.run(node({"user_input": "帮我规划北京三日游"}))

    assert result["pending_tasks"]
    assert result["pending_tasks"][0].task_type is TaskType.PLANNING
