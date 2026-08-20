"""提供第一阶段用于验证路由链路的 Fake 子图。"""

import logging
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


class FakeSubgraphState(TypedDict):
    """Fake 子图只读取用户输入并产生一条固定响应。"""

    user_input: str
    response: NotRequired[str]


def _build_fake_subgraph(response: str) -> CompiledStateGraph:
    """构建一个仅返回固定结果的最小可执行子图。"""

    def respond(_state: FakeSubgraphState) -> dict[str, str]:
        logger.info("Fake子图返回 response=%s", response)
        return {"response": response}

    return (
        StateGraph(FakeSubgraphState)
        .add_node("respond", respond)
        .add_edge(START, "respond")
        .add_edge("respond", END)
        .compile()
    )


inspiration_fake_graph = _build_fake_subgraph("已进入 Fake Inspiration 子图")
unsupported_fake_graph = _build_fake_subgraph("当前暂不支持该请求")
