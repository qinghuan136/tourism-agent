"""验证消息接口的路由、恢复、确认和幂等行为。"""

import asyncio
import logging
from datetime import UTC, datetime
from importlib import import_module
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda

from tourism_agent.models.context import ConversationMessage, ConversationRole
from tourism_agent.models.contracts import IntentDecision
from tourism_agent.models.idempotency import IdempotencyRecord, IdempotencyStatus
from tourism_agent.repositories.idempotency import IdempotencyClaim

USER_ID = UUID("77777777-7777-7777-7777-777777777777")
TRIP_ID = UUID("88888888-8888-8888-8888-888888888888")
IDEMPOTENCY_ID_1 = UUID("99999999-9999-9999-9999-999999999991")
IDEMPOTENCY_ID_2 = UUID("99999999-9999-9999-9999-999999999992")
CANDIDATE_ITINERARY = "第一天西湖，第二天灵隐寺，第三天京杭大运河。"


def message_payload(
    message: str,
    idempotency_id: UUID = IDEMPOTENCY_ID_1,
) -> dict[str, str]:
    """构造带前端幂等 ID 的消息请求。"""
    return {
        "user_id": str(USER_ID),
        "trip_id": str(TRIP_ID),
        "idempotency_id": str(idempotency_id),
        "message": message,
    }


class ApiFakeIdempotencyRepository:
    """在 API 测试中模拟数据库的幂等认领与终态响应存储。"""

    def __init__(self) -> None:
        self.records: dict[UUID, IdempotencyRecord] = {}

    async def claim(
        self,
        idempotency_id: UUID,
        *,
        user_id: UUID,
        trip_id: UUID,
        request_hash: str,
    ) -> IdempotencyClaim:
        existing = self.records.get(idempotency_id)
        if existing is not None:
            return IdempotencyClaim(record=existing, created=False)
        record = IdempotencyRecord(
            idempotency_id=idempotency_id,
            user_id=user_id,
            trip_id=trip_id,
            request_hash=request_hash,
            status=IdempotencyStatus.PROCESSING,
        )
        self.records[idempotency_id] = record
        return IdempotencyClaim(record=record, created=True)

    async def finish(
        self,
        idempotency_id: UUID,
        *,
        status: IdempotencyStatus,
        response_status: int,
        response_body: dict[str, Any],
    ) -> IdempotencyRecord:
        record = self.records[idempotency_id].model_copy(
            update={
                "status": status,
                "response_status": response_status,
                "response_body": response_body,
            }
        )
        self.records[idempotency_id] = record
        return record


class AlreadyProcessingIdempotencyRepository(ApiFakeIdempotencyRepository):
    """模拟同一幂等请求已经被另一个 HTTP 调用认领。"""

    async def claim(
        self,
        idempotency_id: UUID,
        *,
        user_id: UUID,
        trip_id: UUID,
        request_hash: str,
    ) -> IdempotencyClaim:
        return IdempotencyClaim(
            record=IdempotencyRecord(
                idempotency_id=idempotency_id,
                user_id=user_id,
                trip_id=trip_id,
                request_hash=request_hash,
                status=IdempotencyStatus.PROCESSING,
            ),
            created=False,
        )


class FinishFailingIdempotencyRepository(ApiFakeIdempotencyRepository):
    """模拟图运行后幂等终态写入持续失败。"""

    async def finish(
        self,
        idempotency_id: UUID,
        *,
        status: IdempotencyStatus,
        response_status: int,
        response_body: dict[str, Any],
    ) -> IdempotencyRecord:
        raise RuntimeError("模拟幂等终态写入失败")


def override_api_dependencies(
    api: Any,
    graph: Any,
    repository: "ApiFakeRepository",
    idempotency_repository: ApiFakeIdempotencyRepository,
) -> None:
    """让消息 API 测试只替换外部数据库与模型边界。"""
    api.app.dependency_overrides[api.get_root_graph] = lambda: graph
    api.app.dependency_overrides[api.get_planning_repository] = lambda: repository
    api.app.dependency_overrides[api.get_idempotency_repository] = (
        lambda: idempotency_repository
    )


class ApiFakeRepository:
    """模拟 API 所需的旅行归属、Conversation 追加和 Planning 快照读取。"""

    def __init__(self, belongs: bool = True) -> None:
        self.belongs = belongs
        self.messages: list[ConversationMessage] = []
        self.current_itinerary: str | None = None

    async def trip_belongs_to_user(self, user_id: UUID, trip_id: UUID) -> bool:
        assert user_id == USER_ID
        assert trip_id == TRIP_ID
        return self.belongs

    async def append_conversation(
        self,
        trip_id: UUID,
        role: ConversationRole,
        content: str,
    ) -> ConversationMessage:
        message = ConversationMessage(
            id=len(self.messages) + 1,
            role=role,
            content=content,
            created_at=datetime(2026, 8, 16, tzinfo=UTC),
        )
        self.messages.append(message)
        return message

    async def get_recent_conversation(
        self,
        trip_id: UUID,
        *,
        before_message_id: int,
        limit: int,
    ) -> list[ConversationMessage]:
        return [message for message in self.messages if message.id < before_message_id][-limit:]

    async def get_trip_context(self, trip_id: UUID) -> dict[str, object]:
        return {}

    async def get_current_itinerary(self, trip_id: UUID) -> str | None:
        return self.current_itinerary

    async def write_current_itinerary(self, trip_id: UUID, content: str) -> str:
        self.current_itinerary = content
        return content


class FakeBrowserLifecycleClient:
    """记录 API 是否在终态或取消时释放 thread 浏览器会话。"""

    def __init__(self) -> None:
        self.closed_threads: list[str] = []

    async def close_thread(self, thread_id: str) -> None:
        self.closed_threads.append(thread_id)


class AssistantWriteFailingRepository(ApiFakeRepository):
    """首次写入 Agent 可见问题时失败，用于验证 checkpoint 回滚。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_assistant_write = True

    async def append_conversation(
        self,
        trip_id: UUID,
        role: ConversationRole,
        content: str,
    ) -> ConversationMessage:
        if role == ConversationRole.ASSISTANT and self.fail_assistant_write:
            self.fail_assistant_write = False
            raise RuntimeError("模拟 Assistant Conversation 写入失败")
        return await super().append_conversation(trip_id, role, content)


class SemanticFakeModel:
    """同时模拟根图路由和 Planning 最终回答，避免依赖外部 LLM。"""

    def with_structured_output(self, schema: type[IntentDecision]) -> RunnableLambda:
        def decide(messages: list[BaseMessage]) -> Any:
            user_input = str(messages[-1].content)
            if "规划" in user_input:
                route = "planning"
            elif "灵感" in user_input:
                route = "explore"
            else:
                route = "helper"
            return schema(route=route)

        return RunnableLambda(decide)

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            user_input = str(messages[-1].content)
            if user_input.startswith("【当前消息】"):
                current = user_input.split("\n", maxsplit=1)[-1]
                module = "Explore" if "灵感" in current else "Helper"
                return AIMessage(content=f"{module} Agent 已处理：{current}")
            return AIMessage(content=f"Planning Agent 已处理：{user_input}")

        return RunnableLambda(respond)


class QuestionAskingFakeModel(SemanticFakeModel):
    """在 Planning 中主动询问预算，并在恢复后生成最终回答。"""

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            tool_answers = [message for message in messages if message.type == "tool"]
            if tool_answers:
                return AIMessage(content=f"已收到预算：{tool_answers[-1].content}")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {"question": "你的旅行预算是多少？"},
                        "id": "ask-budget-1",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)


class ItineraryConfirmingFakeModel(SemanticFakeModel):
    """只提交候选方案，确认与写入由确定性节点完成。"""

    def __init__(self) -> None:
        self.call_count = 0

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        def respond(_messages: list[BaseMessage]) -> AIMessage:
            self.call_count += 1
            if self.call_count > 1:
                raise AssertionError("确认和写入不应再次调用模型")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_candidate_itinerary",
                        "args": {"itinerary": CANDIDATE_ITINERARY},
                        "id": "submit-api-candidate-1",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)


class MixedThenSequentialCandidateFakeModel(SemanticFakeModel):
    """先违规混用候选与询问 Tool，收到拒绝后改为分轮调用。"""

    def bind_tools(self, _tools: list[object]) -> RunnableLambda:
        def respond(messages: list[BaseMessage]) -> AIMessage:
            tool_messages = [message for message in messages if message.type == "tool"]
            if any(
                "本批次所有 Tool 均未执行" in str(message.content)
                for message in tool_messages
            ):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "submit_candidate_itinerary",
                            "args": {"itinerary": CANDIDATE_ITINERARY},
                            "id": "sequential-api-candidate-1",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_candidate_itinerary",
                        "args": {"itinerary": CANDIDATE_ITINERARY},
                        "id": "concurrent-api-candidate-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "ask_user",
                        "args": {"question": "是否确认这份行程？"},
                        "id": "concurrent-api-confirm-1",
                        "type": "tool_call",
                    },
                ],
            )

        return RunnableLambda(respond)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "帮我规划北京三日游",
            {
                "route": "planning",
                "message": "Planning Agent 已处理：帮我规划北京三日游",
                "candidate_itinerary": None,
                "current_itinerary": None,
            },
        ),
        (
            "给我一些海岛旅行灵感",
            {
                "route": "explore",
                "message": "Explore Agent 已处理：给我一些海岛旅行灵感",
                "candidate_itinerary": None,
                "current_itinerary": None,
            },
        ),
        (
            "帮我写一段排序代码",
            {
                "route": "helper",
                "message": "Helper Agent 已处理：帮我写一段排序代码",
                "candidate_itinerary": None,
                "current_itinerary": None,
            },
        ),
    ],
)
def test_message_endpoint_routes_request_to_expected_subgraph(
    message: str,
    expected: dict[str, str | None],
    caplog,
) -> None:
    """消息接口必须按照理解结果进入对应子图，而不是由 API 自行分支。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(SemanticFakeModel(), repository)
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        with caplog.at_level(logging.INFO, logger="tourism_agent.api"):
            response = TestClient(api.app).post(
                "/messages",
                json=message_payload(message),
            )
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == expected
    assert [saved.role for saved in repository.messages] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]
    assert repository.messages[0].content == message
    assert repository.messages[1].content == expected["message"]
    assert "API收到消息" in caplog.text
    assert "API消息处理完成" in caplog.text


def test_message_endpoint_rejects_trip_outside_current_user_scope() -> None:
    """API 必须在写入 Conversation 和运行根图之前验证旅行归属。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository(belongs=False)
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(SemanticFakeModel(), repository)
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        response = TestClient(api.app).post(
            "/messages",
            json=message_payload("规划杭州旅行"),
        )
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "未找到当前用户对应的旅行"}
    assert repository.messages == []


def test_successful_message_clears_completed_checkpoint() -> None:
    """正常响应完成后应删除本轮内存 checkpoint，避免历史持续累积。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(SemanticFakeModel(), repository)
    browser_client = FakeBrowserLifecycleClient()
    api.app.state.browser_client = browser_client
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        response = TestClient(api.app).post(
            "/messages",
            json=message_payload("帮我规划北京三日游"),
        )
        snapshot = asyncio.run(
            graph.aget_state(api.graph_config(str(TRIP_ID)))
        )
    finally:
        api.app.dependency_overrides.clear()
        del api.app.state.browser_client

    assert response.status_code == 200
    assert snapshot.values == {}
    assert snapshot.interrupts == ()
    assert browser_client.closed_threads == [str(TRIP_ID)]


def test_root_graph_config_leaves_headroom_for_helper_react_budget() -> None:
    """根图上限应高于 Helper 的独立 ReAct 上限，避免正常收束前被框架中断。"""
    api = import_module("tourism_agent.api")

    assert api.graph_config(str(TRIP_ID))["recursion_limit"] == 50


def test_message_endpoint_resumes_pending_agent_question() -> None:
    """待回答的 thread 收到消息时应恢复 interrupt，不重新运行理解节点。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(QuestionAskingFakeModel(), repository)
    browser_client = FakeBrowserLifecycleClient()
    api.app.state.browser_client = browser_client
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        client = TestClient(api.app)
        first = client.post(
            "/messages",
            json=message_payload("帮我规划杭州旅行", IDEMPOTENCY_ID_1),
        )
        assert browser_client.closed_threads == [str(TRIP_ID)]
        second = client.post(
            "/messages",
            json=message_payload("5000元", IDEMPOTENCY_ID_2),
        )
    finally:
        api.app.dependency_overrides.clear()
        del api.app.state.browser_client

    assert first.status_code == 200
    assert first.json() == {
        "route": "planning",
        "message": "你的旅行预算是多少？",
        "candidate_itinerary": None,
        "current_itinerary": None,
    }
    assert second.status_code == 200
    assert second.json() == {
        "route": "planning",
        "message": "已收到预算：5000元",
        "candidate_itinerary": None,
        "current_itinerary": None,
    }
    assert [message.role for message in repository.messages] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]
    assert browser_client.closed_threads == [str(TRIP_ID), str(TRIP_ID)]


def test_cancel_pending_question_makes_next_message_start_from_root() -> None:
    """取消等待中的 checkpoint 后，下一条消息必须重新进入根图。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(QuestionAskingFakeModel(), repository)
    browser_client = FakeBrowserLifecycleClient()
    api.app.state.browser_client = browser_client
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        client = TestClient(api.app)
        client.post(
            "/messages",
            json=message_payload("帮我规划杭州旅行", IDEMPOTENCY_ID_1),
        )
        cancelled = client.post(
            f"/trips/{TRIP_ID}/cancel",
            json={"user_id": str(USER_ID)},
        )
        restarted = client.post(
            "/messages",
            json=message_payload("重新帮我规划杭州旅行", IDEMPOTENCY_ID_2),
        )
    finally:
        api.app.dependency_overrides.clear()
        del api.app.state.browser_client

    assert cancelled.status_code == 200
    assert cancelled.json() == {"cancelled": True}
    assert browser_client.closed_threads == [
        str(TRIP_ID),
        str(TRIP_ID),
        str(TRIP_ID),
    ]
    assert restarted.status_code == 200
    assert restarted.json() == {
        "route": "planning",
        "message": "你的旅行预算是多少？",
        "candidate_itinerary": None,
        "current_itinerary": None,
    }


def test_failed_question_persistence_discards_pending_checkpoint() -> None:
    """用户没有看到 Agent 问题时，后续消息不得错误恢复该 interrupt。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = AssistantWriteFailingRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(QuestionAskingFakeModel(), repository)
    browser_client = FakeBrowserLifecycleClient()
    api.app.state.browser_client = browser_client
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        client = TestClient(api.app, raise_server_exceptions=False)
        failed = client.post(
            "/messages",
            json=message_payload("帮我规划杭州旅行", IDEMPOTENCY_ID_1),
        )
        restarted = client.post(
            "/messages",
            json=message_payload("重新帮我规划杭州旅行", IDEMPOTENCY_ID_2),
        )
    finally:
        api.app.dependency_overrides.clear()
        del api.app.state.browser_client

    assert failed.status_code == 500
    assert restarted.status_code == 200
    assert restarted.json() == {
        "route": "planning",
        "message": "你的旅行预算是多少？",
        "candidate_itinerary": None,
        "current_itinerary": None,
    }
    assert browser_client.closed_threads == [str(TRIP_ID), str(TRIP_ID)]


def test_browser_session_is_closed_when_idempotency_finish_fails() -> None:
    """图已经启动后，即使幂等结果写入失败也必须释放浏览器会话。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = FinishFailingIdempotencyRepository()
    graph = root_graph.build_root_graph(SemanticFakeModel(), repository)
    browser_client = FakeBrowserLifecycleClient()
    api.app.state.browser_client = browser_client
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        response = TestClient(api.app, raise_server_exceptions=False).post(
            "/messages",
            json=message_payload("帮我规划北京三日游"),
        )
    finally:
        api.app.dependency_overrides.clear()
        del api.app.state.browser_client

    assert response.status_code == 500
    assert browser_client.closed_threads == [str(TRIP_ID)]


def test_api_returns_itinerary_separately_without_polluting_conversation() -> None:
    """完整候选和当前行程应走独立字段，Conversation 只保存简短可见消息。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(ItineraryConfirmingFakeModel(), repository)
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        client = TestClient(api.app)
        candidate = client.post(
            "/messages",
            json=message_payload("帮我规划杭州三日行程", IDEMPOTENCY_ID_1),
        )
        duplicate_candidate = client.post(
            "/messages",
            json=message_payload("帮我规划杭州三日行程", IDEMPOTENCY_ID_1),
        )
        confirmed = client.post(
            "/messages",
            json=message_payload("是", IDEMPOTENCY_ID_2),
        )
    finally:
        api.app.dependency_overrides.clear()

    assert candidate.status_code == 200
    assert candidate.json() == {
        "route": "planning",
        "message": "是否确认采用这份行程？请选择：是或否。",
        "candidate_itinerary": CANDIDATE_ITINERARY,
        "current_itinerary": None,
    }
    assert duplicate_candidate.status_code == 200
    assert duplicate_candidate.json() == candidate.json()
    assert confirmed.status_code == 200
    assert confirmed.json() == {
        "route": "planning",
        "message": "已保存你确认的行程。",
        "candidate_itinerary": None,
        "current_itinerary": CANDIDATE_ITINERARY,
    }
    assert repository.current_itinerary == CANDIDATE_ITINERARY
    assert CANDIDATE_ITINERARY not in "\n".join(
        message.content for message in repository.messages
    )


def test_api_returns_candidate_after_model_corrects_mixed_tool_calls() -> None:
    """API 应在模型把混合调用拆分后返回候选方案。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(
        MixedThenSequentialCandidateFakeModel(),
        repository,
    )
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        response = TestClient(api.app).post(
            "/messages",
            json=message_payload("帮我规划杭州三日行程"),
        )
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "route": "planning",
        "message": "是否确认采用这份行程？请选择：是或否。",
        "candidate_itinerary": CANDIDATE_ITINERARY,
        "current_itinerary": None,
    }


def test_api_rejects_non_binary_candidate_confirmation_without_persisting() -> None:
    """候选确认只接受“是”或“否”，无效输入不能写入 Conversation。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(ItineraryConfirmingFakeModel(), repository)
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        client = TestClient(api.app)
        candidate = client.post(
            "/messages",
            json=message_payload("帮我规划杭州三日行程", IDEMPOTENCY_ID_1),
        )
        message_count = len(repository.messages)
        invalid = client.post(
            "/messages",
            json=message_payload("差不多可以", IDEMPOTENCY_ID_2),
        )
    finally:
        api.app.dependency_overrides.clear()

    assert candidate.status_code == 200
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "候选方案确认只接受“是”或“否”"
    assert len(repository.messages) == message_count
    assert repository.current_itinerary is None


def test_completed_idempotent_request_replays_response_without_side_effects() -> None:
    """重复提交已完成请求时必须重放响应，不能再次写对话或运行根图。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(SemanticFakeModel(), repository)
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        client = TestClient(api.app)
        first = client.post(
            "/messages",
            json=message_payload("帮我规划北京三日游"),
        )
        duplicate = client.post(
            "/messages",
            json=message_payload("帮我规划北京三日游"),
        )
    finally:
        api.app.dependency_overrides.clear()

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json() == first.json()
    assert len(repository.messages) == 2


def test_processing_idempotent_request_returns_accepted_without_running_graph() -> None:
    """相同请求仍在执行时返回 processing，不能落入 thread 忙碌分支。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = AlreadyProcessingIdempotencyRepository()
    graph = root_graph.build_root_graph(SemanticFakeModel(), repository)
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        response = TestClient(api.app).post(
            "/messages",
            json=message_payload("帮我规划北京三日游"),
        )
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json() == {
        "idempotency_id": str(IDEMPOTENCY_ID_1),
        "status": "processing",
    }
    assert repository.messages == []


def test_reused_idempotency_id_with_changed_payload_is_rejected() -> None:
    """同一幂等 ID 被用于不同消息时必须返回冲突，避免错误重放。"""
    api = import_module("tourism_agent.api")
    root_graph = import_module("tourism_agent.graph.root")
    repository = ApiFakeRepository()
    idempotency_repository = ApiFakeIdempotencyRepository()
    graph = root_graph.build_root_graph(SemanticFakeModel(), repository)
    override_api_dependencies(api, graph, repository, idempotency_repository)

    try:
        client = TestClient(api.app)
        first = client.post(
            "/messages",
            json=message_payload("帮我规划北京三日游"),
        )
        conflict = client.post(
            "/messages",
            json=message_payload("给我一些海岛旅行灵感"),
        )
    finally:
        api.app.dependency_overrides.clear()

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "idempotency_id 已用于不同的请求内容"}
    assert len(repository.messages) == 2
