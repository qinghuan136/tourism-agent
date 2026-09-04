"""定义 Helper 子图单次运行使用的最小工作状态。"""

from typing import Annotated, Any, NotRequired, TypedDict
from uuid import UUID

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from tourism_agent.models.context import ConversationMessage
from tourism_agent.models.rag import ConversationChunkMatch


class HelperState(TypedDict):
    """保存轻量辅助任务所需的只读快照、ReAct 消息和最终回答。"""

    user_id: UUID
    trip_id: UUID
    user_message_id: int
    retrieval_query: NotRequired[str]
    retrieval_user_input: NotRequired[str]
    retrieval_task_goal: NotRequired[str]
    messages: Annotated[list[AnyMessage], add_messages]
    conversation_context: NotRequired[list[ConversationMessage]]
    retrieved_history: NotRequired[list[ConversationChunkMatch]]
    trip_context: NotRequired[dict[str, Any]]
    current_itinerary: NotRequired[str | None]
    itinerary_committed_this_request: NotRequired[bool]
    react_round_count: NotRequired[int]
    assistant_message: NotRequired[str]
