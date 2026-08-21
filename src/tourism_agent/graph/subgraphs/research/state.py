"""定义 Research 子图的结构化计划与单次运行状态。"""

from typing import Annotated, Any, NotRequired, TypedDict
from uuid import UUID

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from tourism_agent.models.context import ConversationMessage


def _validate_meaningful_text(value: str) -> str:
    """拒绝只有逗号、横线等符号的模型占位内容。"""
    if not any(character.isalnum() for character in value):
        raise ValueError("内容必须包含中文、字母或数字")
    return value


MeaningfulText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2),
    AfterValidator(_validate_meaningful_text),
]
OptionalNote = Annotated[str, StringConstraints(strip_whitespace=True)]


class ResearchPlan(BaseModel):
    """描述本轮研究目标、任务清单、来源策略、结束标准和整体备注。"""

    goal: MeaningfulText = Field(description="本轮研究需要回答的核心目标")
    tasks: list[MeaningfulText] = Field(
        min_length=2,
        max_length=6,
        description="具体且自包含的研究任务清单",
    )
    source_strategy: list[MeaningfulText] = Field(
        min_length=1,
        max_length=5,
        description="优先来源类型和交叉核查策略",
    )
    success_criteria: list[MeaningfulText] = Field(
        min_length=1,
        max_length=5,
        description="判断研究信息已经足够的可检查标准",
    )
    notes: OptionalNote = Field(
        description="适用于整个计划的补充说明；没有时使用空字符串",
    )


class ResearchState(TypedDict):
    """保存一次深度研究所需的只读快照、计划与 ReAct 消息。"""

    user_id: UUID
    trip_id: UUID
    user_message_id: int
    messages: Annotated[list[AnyMessage], add_messages]
    conversation_context: NotRequired[list[ConversationMessage]]
    trip_context: NotRequired[dict[str, Any]]
    current_itinerary: NotRequired[str | None]
    research_plan: NotRequired[ResearchPlan]
    replan_reason: NotRequired[str | None]
    plan_revision_count: NotRequired[int]
    assistant_message: NotRequired[str]
