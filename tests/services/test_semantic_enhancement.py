"""验证 RAG 查询和 Chunk 的语义增强边界。"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableLambda

from tourism_agent.models.context import ConversationMessage, ConversationRole


class FakeEnhancementModel:
    """返回固定结构化文本，并记录模型实际收到的消息。"""

    def __init__(self, retrieval_text: str) -> None:
        self.retrieval_text = retrieval_text
        self.calls: list[list[BaseMessage]] = []

    def with_structured_output(self, schema: type[Any]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> Any:
            self.calls.append(messages)
            return schema(retrieval_text=self.retrieval_text)

        return RunnableLambda(respond)


def recent_conversation() -> list[ConversationMessage]:
    """构造五条历史，验证增强 Service 只使用最后四条。"""
    created_at = datetime(2026, 9, 1, tzinfo=UTC)
    return [
        ConversationMessage(
            id=index,
            role=(ConversationRole.USER if index % 2 else ConversationRole.ASSISTANT),
            content=f"历史消息{index}",
            created_at=created_at,
        )
        for index in range(1, 6)
    ]


def test_query_enhancement_returns_complete_model_text_with_labeled_context() -> None:
    """缺少用户输入、Task 目标或近期历史会使指代补全失去依据。"""
    from tourism_agent.services.semantic_enhancement import (
        SemanticEnhancementService,
    )

    model = FakeEnhancementModel("查询此前约1000元预算的广州旅行方案。")
    service = SemanticEnhancementService(model, model_name="demo-chat-model")

    result = asyncio.run(
        service.enhance_query(
            query="之前那个便宜点的方案",
            current_user_input="还是之前广州那个便宜点的方案。",
            task_goal="召回用户此前讨论过的广州低预算旅行方案。",
            recent_conversation=recent_conversation(),
        )
    )

    assert result == "查询此前约1000元预算的广州旅行方案。"
    assert service.model_name == "demo-chat-model"
    prompt = str(model.calls[0][-1].content)
    assert "【原始查询】\n之前那个便宜点的方案" in prompt
    assert "【当前用户输入】\n还是之前广州那个便宜点的方案。" in prompt
    assert "【当前Task目标】\n召回用户此前讨论过的广州低预算旅行方案。" in prompt
    assert "历史消息1" not in prompt
    for index in range(2, 6):
        assert f"历史消息{index}" in prompt


def test_exchange_enhancement_returns_complete_retrieval_text() -> None:
    """Chunk 必须使用模型生成的完整增强文本，而不是由 Service 拼接原文。"""
    from tourism_agent.services.semantic_enhancement import (
        SemanticEnhancementService,
    )

    model = FakeEnhancementModel(
        "用户选择沙面方案并希望住宿价格较低，建议住在沙面附近。"
    )
    service = SemanticEnhancementService(model, model_name="demo-chat-model")

    result = asyncio.run(
        service.enhance_exchange(
            user_message="第二个吧，酒店不要太贵。",
            assistant_message="建议选择沙面附近的经济型酒店。",
            context_goal="根据用户选择调整广州行程的住宿区域。",
            recent_conversation=recent_conversation(),
        )
    )

    assert result == "用户选择沙面方案并希望住宿价格较低，建议住在沙面附近。"
    prompt = str(model.calls[0][-1].content)
    assert "【当前用户消息】\n第二个吧，酒店不要太贵。" in prompt
    assert "【当前Assistant消息】\n建议选择沙面附近的经济型酒店。" in prompt
    assert "【本次上下文目标】\n根据用户选择调整广州行程的住宿区域。" in prompt
    assert "历史消息1" not in prompt

