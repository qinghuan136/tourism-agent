"""提供 Tourism Agent 的 FastAPI 应用入口。"""

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from tourism_agent.graph.root import build_root_graph
from tourism_agent.graph.tools.travel_query import create_query_tools
from tourism_agent.infrastructure.database import DatabaseSettings, PostgresDatabase
from tourism_agent.infrastructure.logging_config import (
    LoggingSettings,
    configure_logging,
    log_preview,
    shutdown_logging,
)
from tourism_agent.models.context import ConversationRole
from tourism_agent.models.contracts import (
    CancelRunRequest,
    CancelRunResponse,
    IdempotencyProcessingResponse,
    MessageRequest,
    MessageResponse,
)
from tourism_agent.models.idempotency import IdempotencyRecord, IdempotencyStatus
from tourism_agent.providers.model import create_chat_model
from tourism_agent.providers.travel import TravelToolSettings, open_travel_query_clients
from tourism_agent.repositories.idempotency import IdempotencyRepository
from tourism_agent.repositories.planning import PlanningRepository
from tourism_agent.services.run_control import (
    ThreadBusyError,
    ThreadRunCancelledError,
    ThreadRunCoordinator,
)

ROOT_GRAPH_RECURSION_LIMIT = 50
logger = logging.getLogger(__name__)


@lru_cache
def get_database() -> PostgresDatabase:
    """创建应用级共享数据库连接池。"""
    return PostgresDatabase(DatabaseSettings())


@lru_cache
def get_planning_repository() -> PlanningRepository:
    """复用无状态 Repository，统一访问 Planning 业务表。"""
    return PlanningRepository(get_database())


@lru_cache
def get_idempotency_repository() -> IdempotencyRepository:
    """复用无状态 Repository，统一访问消息请求幂等记录。"""
    return IdempotencyRepository(get_database())


def get_root_graph(request: Request) -> CompiledStateGraph:
    """取得应用启动时组装的共享根图。"""
    return request.app.state.root_graph


@lru_cache
def get_run_coordinator() -> ThreadRunCoordinator:
    """复用进程内 thread_id 锁和活动任务记录。"""
    return ThreadRunCoordinator()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """统一管理数据库、查询客户端、MCP 会话和根图生命周期。"""
    configure_logging(LoggingSettings())
    logger.info("应用启动开始")
    database = get_database()
    await database.open()
    logger.info("PostgreSQL连接池已打开")
    try:
        repository = get_planning_repository()
        settings = TravelToolSettings()
        logger.info("旅行查询配置加载完成")
        async with open_travel_query_clients(settings) as clients:
            public_query_tools = create_query_tools(
                clients.weather,
                clients.places,
                clients.web_search,
                clients.routes,
            )
            # 根图、MCP 会话与 HTTP 客户端均由单一启动协程创建并在进程内复用。
            _app.state.root_graph = build_root_graph(
                create_chat_model(),
                repository,
                query_tools=public_query_tools,
            )
            logger.info(
                "根图创建完成 public_query_tools=%s",
                [tool.name for tool in public_query_tools],
            )
            get_run_coordinator()
            logger.info("应用启动完成")
            try:
                yield
            finally:
                del _app.state.root_graph
    finally:
        get_run_coordinator.cache_clear()
        get_idempotency_repository.cache_clear()
        get_planning_repository.cache_clear()
        await database.close()
        logger.info("PostgreSQL连接池已关闭")
        get_database.cache_clear()
        logger.info("应用关闭完成")
        shutdown_logging()


app = FastAPI(title="Tourism Agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """返回最小健康状态，供本地运行和后续部署检查使用。"""
    return {"status": "ok"}


@app.post(
    "/messages",
    response_model=MessageResponse | IdempotencyProcessingResponse,
)
async def handle_message(
    request: MessageRequest,
    graph: Annotated[CompiledStateGraph, Depends(get_root_graph)],
    repository: Annotated[PlanningRepository, Depends(get_planning_repository)],
    idempotency_repository: Annotated[
        IdempotencyRepository,
        Depends(get_idempotency_repository),
    ],
    coordinator: Annotated[ThreadRunCoordinator, Depends(get_run_coordinator)],
) -> MessageResponse | Response:
    """根据 checkpoint 决定启动根图或恢复 Agent 提问。"""
    logger.info(
        "API收到消息 user_id=%s trip_id=%s idempotency_id=%s message=%s",
        request.user_id,
        request.trip_id,
        request.idempotency_id,
        log_preview(request.message),
    )
    belongs = await repository.trip_belongs_to_user(request.user_id, request.trip_id)
    if not belongs:
        logger.warning(
            "API拒绝消息：旅行不属于当前用户 user_id=%s trip_id=%s",
            request.user_id,
            request.trip_id,
        )
        raise HTTPException(status_code=404, detail="未找到当前用户对应的旅行")

    request_hash = build_request_hash(request)
    claim = await idempotency_repository.claim(
        request.idempotency_id,
        user_id=request.user_id,
        trip_id=request.trip_id,
        request_hash=request_hash,
    )
    if not claim.created:
        return replay_idempotent_request(claim.record, request, request_hash)

    thread_id = str(request.trip_id)
    config = graph_config(thread_id)
    user_message_id = 0

    async def persist_user_input() -> None:
        nonlocal user_message_id
        snapshot = await graph.aget_state(config)
        if snapshot.interrupts:
            interrupt_value = snapshot.interrupts[0].value
            if (
                interrupt_value.get("kind") == "candidate_confirmation"
                and request.message not in {"是", "否"}
            ):
                logger.warning(
                    "API拒绝无效候选确认 trip_id=%s message=%s",
                    request.trip_id,
                    log_preview(request.message),
                )
                raise HTTPException(
                    status_code=422,
                    detail="候选方案确认只接受“是”或“否”",
                )
        user_message = await repository.append_conversation(
            request.trip_id,
            ConversationRole.USER,
            request.message,
        )
        user_message_id = user_message.id
        logger.info(
            "用户消息已写入Conversation trip_id=%s user_message_id=%s",
            request.trip_id,
            user_message_id,
        )

    async def run_graph() -> MessageResponse:
        snapshot = await graph.aget_state(config)
        graph_input: dict[str, object] | Command
        if snapshot.interrupts:
            logger.info("恢复等待中的根图 trip_id=%s", request.trip_id)
            graph_input = Command(resume=request.message)
        else:
            logger.info("启动新的根图运行 trip_id=%s", request.trip_id)
            graph_input = {
                "user_id": request.user_id,
                "trip_id": request.trip_id,
                "user_message_id": user_message_id,
                "user_input": request.message,
            }

        result = await graph.ainvoke(graph_input, config)
        interrupted = bool(result.get("__interrupt__"))
        logger.info(
            "根图运行返回 trip_id=%s route=%s interrupted=%s",
            request.trip_id,
            result.get("route"),
            interrupted,
        )
        response_committed = False
        try:
            response_message = get_user_visible_message(result)
            await repository.append_conversation(
                request.trip_id,
                ConversationRole.ASSISTANT,
                response_message,
            )
            response_committed = True
            logger.info(
                "Assistant消息已写入Conversation trip_id=%s message=%s",
                request.trip_id,
                log_preview(response_message),
            )
        finally:
            # 用户未看到提问时不能保留可恢复的 interrupt，否则重试会被误当作回答。
            if interrupted and not response_committed:
                logger.warning("响应写入失败，清除interrupt checkpoint trip_id=%s", request.trip_id)
                await graph.checkpointer.adelete_thread(thread_id)
        response = MessageResponse(
            route=result["route"],
            message=response_message,
            candidate_itinerary=get_candidate_itinerary(result),
            current_itinerary=await repository.get_current_itinerary(request.trip_id),
        )
        await idempotency_repository.finish(
            request.idempotency_id,
            status=IdempotencyStatus.COMPLETED,
            response_status=200,
            response_body=response.model_dump(mode="json"),
        )
        if not interrupted:
            # 先保存可重放响应，再清理 GraphState，避免重试落入新的图运行。
            await graph.checkpointer.adelete_thread(thread_id)
            logger.info("正常运行结束，已清理checkpoint trip_id=%s", request.trip_id)
        logger.info(
            "API消息处理完成 trip_id=%s route=%s has_candidate=%s has_current_itinerary=%s",
            request.trip_id,
            response.route.value,
            response.candidate_itinerary is not None,
            response.current_itinerary is not None,
        )
        return response

    try:
        return await coordinator.execute(
            thread_id,
            run_graph,
            before_cancellation=persist_user_input,
        )
    except ThreadBusyError as exc:
        logger.warning("API拒绝并发消息 trip_id=%s", request.trip_id)
        await save_idempotent_error(
            idempotency_repository,
            request.idempotency_id,
            status=IdempotencyStatus.FAILED,
            response_status=409,
            detail="当前旅行正在处理中，请先取消",
        )
        raise HTTPException(status_code=409, detail="当前旅行正在处理中，请先取消") from exc
    except ThreadRunCancelledError as exc:
        logger.info("API运行已取消 trip_id=%s", request.trip_id)
        await save_idempotent_error(
            idempotency_repository,
            request.idempotency_id,
            status=IdempotencyStatus.CANCELLED,
            response_status=409,
            detail="当前运行已取消",
        )
        raise HTTPException(status_code=409, detail="当前运行已取消") from exc
    except HTTPException as exc:
        await save_idempotent_error(
            idempotency_repository,
            request.idempotency_id,
            status=IdempotencyStatus.FAILED,
            response_status=exc.status_code,
            detail=exc.detail,
        )
        raise
    except Exception:
        logger.exception(
            "API消息处理异常 trip_id=%s idempotency_id=%s",
            request.trip_id,
            request.idempotency_id,
        )
        body = {"detail": "消息处理失败，请稍后使用新的 idempotency_id 重试"}
        await idempotency_repository.finish(
            request.idempotency_id,
            status=IdempotencyStatus.FAILED,
            response_status=500,
            response_body=body,
        )
        return JSONResponse(status_code=500, content=body)


@app.post("/trips/{trip_id}/cancel", response_model=CancelRunResponse)
async def cancel_run(
    trip_id: UUID,
    request: CancelRunRequest,
    graph: Annotated[CompiledStateGraph, Depends(get_root_graph)],
    repository: Annotated[PlanningRepository, Depends(get_planning_repository)],
    coordinator: Annotated[ThreadRunCoordinator, Depends(get_run_coordinator)],
) -> CancelRunResponse:
    """取消执行中的任务，或清除等待用户回答的 checkpoint。"""
    logger.info("API收到取消请求 user_id=%s trip_id=%s", request.user_id, trip_id)
    belongs = await repository.trip_belongs_to_user(request.user_id, trip_id)
    if not belongs:
        raise HTTPException(status_code=404, detail="未找到当前用户对应的旅行")

    thread_id = str(trip_id)
    async def clear_checkpoint(cancelled_running: bool) -> bool:
        snapshot = await graph.aget_state(graph_config(thread_id))
        cancelled = cancelled_running or bool(snapshot.interrupts)
        if cancelled:
            await graph.checkpointer.adelete_thread(thread_id)
        return cancelled

    cancelled = await coordinator.cancel(thread_id, clear_checkpoint)
    logger.info("API取消处理完成 trip_id=%s cancelled=%s", trip_id, cancelled)
    return CancelRunResponse(cancelled=cancelled)

def graph_config(thread_id: str) -> dict[str, object]:
    """构造根图运行配置；当前一个 trip 对应一个 thread_id。"""
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": ROOT_GRAPH_RECURSION_LIMIT,
    }


def build_request_hash(request: MessageRequest) -> str:
    """对规范化后的请求作用域和消息生成稳定指纹。"""
    canonical = f"{request.user_id}\n{request.trip_id}\n{request.message}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replay_idempotent_request(
    record: IdempotencyRecord,
    request: MessageRequest,
    request_hash: str,
) -> Response:
    """校验幂等 ID 的请求归属，并返回处理中状态或缓存终态。"""
    if (
        record.user_id != request.user_id
        or record.trip_id != request.trip_id
        or record.request_hash != request_hash
    ):
        raise HTTPException(
            status_code=409,
            detail="idempotency_id 已用于不同的请求内容",
        )

    if record.status is IdempotencyStatus.PROCESSING:
        body = IdempotencyProcessingResponse(
            idempotency_id=record.idempotency_id
        ).model_dump(mode="json")
        return JSONResponse(status_code=202, content=body)

    if record.response_status is None or record.response_body is None:
        raise RuntimeError("幂等请求终态缺少可重放的 HTTP 响应")
    return JSONResponse(
        status_code=record.response_status,
        content=record.response_body,
    )


async def save_idempotent_error(
    repository: IdempotencyRepository,
    idempotency_id: UUID,
    *,
    status: IdempotencyStatus,
    response_status: int,
    detail: object,
) -> None:
    """统一保存 FastAPI 错误响应，保证相同请求能够按原状态重放。"""
    await repository.finish(
        idempotency_id,
        status=status,
        response_status=response_status,
        response_body={"detail": detail},
    )


def get_user_visible_message(result: dict[str, object]) -> str:
    """中断时返回 Agent 问题，正常完成时返回根图结果。"""
    interrupts = result.get("__interrupt__", ())
    if interrupts:
        return interrupts[0].value["question"]
    return str(result["response"])


def get_candidate_itinerary(result: dict[str, object]) -> str | None:
    """优先从 interrupt 载荷读取待确认方案，正常结束时读取根图输出。"""
    interrupts = result.get("__interrupt__", ())
    value = (
        interrupts[0].value.get("candidate_itinerary")
        if interrupts
        else result.get("candidate_itinerary")
    )
    return str(value) if value else None
