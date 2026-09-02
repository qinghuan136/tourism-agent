"""验证消息接口、Orchestrator 根图共享的输入输出契约。"""

from uuid import UUID

import pytest
from pydantic import ValidationError

from tourism_agent.models import contracts
from tourism_agent.models.contracts import MessageRequest
from tourism_agent.models.orchestration import TaskType

USER_ID = UUID("55555555-5555-5555-5555-555555555555")
TRIP_ID = UUID("66666666-6666-6666-6666-666666666666")
IDEMPOTENCY_ID = UUID("77777777-7777-7777-7777-777777777777")


def test_message_request_strips_surrounding_whitespace() -> None:
    """API 请求进入系统前应去除消息两端无意义的空白。"""
    request = MessageRequest(
        user_id=USER_ID,
        trip_id=TRIP_ID,
        idempotency_id=IDEMPOTENCY_ID,
        message="  帮我规划北京三日游  ",
    )

    assert request.message == "帮我规划北京三日游"
    assert request.user_id == USER_ID
    assert request.trip_id == TRIP_ID
    assert request.idempotency_id == IDEMPOTENCY_ID


def test_message_request_rejects_blank_message() -> None:
    """纯空白消息无法形成可执行请求，应在 API 契约边界被拒绝。"""
    with pytest.raises(ValidationError):
        MessageRequest(
            user_id=USER_ID,
            trip_id=TRIP_ID,
            idempotency_id=IDEMPOTENCY_ID,
            message="   ",
        )


def test_message_request_accepts_4000_characters() -> None:
    """详细旅行需求不超过 4000 字符时应正常进入系统。"""
    request = MessageRequest(
        user_id=USER_ID,
        trip_id=TRIP_ID,
        idempotency_id=IDEMPOTENCY_ID,
        message="行" * 4000,
    )

    assert len(request.message) == 4000


def test_message_request_rejects_more_than_4000_characters() -> None:
    """超长消息应在 API 契约边界被拒绝，不能进入后续工作流。"""
    with pytest.raises(ValidationError):
        MessageRequest(
            user_id=USER_ID,
            trip_id=TRIP_ID,
            idempotency_id=IDEMPOTENCY_ID,
            message="行" * 4001,
        )


def test_message_request_rejects_invalid_scope_ids() -> None:
    """用户和旅行作用域必须使用合法 UUID，不能把任意字符串传入根图。"""
    with pytest.raises(ValidationError):
        MessageRequest(
            user_id="not-a-uuid",
            trip_id=TRIP_ID,
            idempotency_id=IDEMPOTENCY_ID,
            message="规划杭州旅行",
        )


def test_message_request_requires_frontend_idempotency_id() -> None:
    """消息请求缺少前端生成的幂等 ID 时必须在契约边界被拒绝。"""
    with pytest.raises(ValidationError):
        MessageRequest(user_id=USER_ID, trip_id=TRIP_ID, message="规划杭州旅行")


def test_message_response_serializes_public_api_contract() -> None:
    """API 应分别序列化简短消息、候选方案和已确认行程。"""
    response = contracts.MessageResponse(
        route="planning",
        message="  已进入 Fake Planning 子图  ",
        candidate_itinerary="  杭州三日候选方案  ",
        current_itinerary="  杭州两日已确认方案  ",
    )

    assert response.route is TaskType.PLANNING
    assert response.model_dump(mode="json") == {
        "route": "planning",
        "message": "已进入 Fake Planning 子图",
        "candidate_itinerary": "杭州三日候选方案",
        "current_itinerary": "杭州两日已确认方案",
    }


def test_root_graph_remains_importable() -> None:
    """根图应保持为 API 可调用的工作流入口。"""
    from tourism_agent.graph.root import build_root_graph

    assert callable(build_root_graph)
