"""提供业务 Conversation 到模型消息的共享转换。"""

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from tourism_agent.models.context import ConversationMessage, ConversationRole


def conversation_to_messages(
    conversation: Sequence[ConversationMessage],
    *,
    label: str | None = None,
) -> list[BaseMessage]:
    """保留消息角色，并按调用方需要添加用途标签。"""
    messages: list[BaseMessage] = []
    for message in conversation:
        content = f"{label}\n{message.content}" if label else message.content
        if message.role == ConversationRole.USER:
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    return messages
