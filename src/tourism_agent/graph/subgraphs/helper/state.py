"""定义 Helper 子图单次运行使用的最小工作状态。"""

from typing import Annotated, Any, NotRequired, TypedDict
from uuid import UUID

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from tourism_agent.models.context import ConversationMessage


class HelperState(TypedDict):
    """保存轻量辅助任务所需的只读快照、ReAct 消息和最终回答。"""

    user_id: UUID
    trip_id: UUID
    user_message_id: int
    messages: Annotated[list[AnyMessage], add_messages]
    conversation_context: NotRequired[list[ConversationMessage]]
    trip_context: NotRequired[dict[str, Any]]
    current_itinerary: NotRequired[str | None]
    react_round_count: NotRequired[int]
    assistant_message: NotRequired[str]
