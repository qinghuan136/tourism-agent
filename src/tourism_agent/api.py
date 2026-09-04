"""提供 Tourism Agent 的 FastAPI 应用入口。"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.embeddings import Embeddings
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from tourism_agent.graph.root import build_root_graph
from tourism_agent.graph.tools.conversation_history import (
    create_conversation_history_tools,
)
from tourism_agent.graph.tools.date_time import create_date_time_tools
from tourism_agent.graph.tools.travel_query import create_query_tools
from tourism_agent.infrastructure.database import DatabaseSettings, PostgresDatabase
from tourism_agent.infrastructure.logging_config import (
    LoggingSettings,
    configure_logging,
    log_preview,
    shutdown_logging,
)
from tourism_agent.models.context import ConversationMessage, ConversationRole
from tourism_agent.models.contracts import (
    CancelRunRequest,
    CancelRunResponse,
    ConversationPage,
    IdempotencyProcessingResponse,
    MessageRequest,
    MessageResponse,
    TripBootstrapResponse,
)
from tourism_agent.models.idempotency import IdempotencyRecord, IdempotencyStatus
from tourism_agent.models.orchestration import TaskSpec, TaskType
from tourism_agent.providers.model import (
    ModelSettings,
    create_chat_model,
    create_embedding_model,
)
from tourism_agent.providers.reranker import QwenTextReranker, create_qwen_reranker
from tourism_agent.providers.travel import TravelToolSettings, open_travel_query_clients
from tourism_agent.repositories.idempotency import IdempotencyRepository
from tourism_agent.repositories.planning import PlanningRepository
from tourism_agent.services.conversation_chunk import ConversationChunkService
from tourism_agent.services.conversation_retrieval import (
    ConversationRetrievalService,
)
from tourism_agent.services.run_control import (
    ThreadBusyError,
    ThreadRunCancelledError,
    ThreadRunCoordinator,
)
from tourism_agent.services.semantic_enhancement import SemanticEnhancementService
from tourism_agent.services.sse_stream import (
    GraphEventTranslator,
    SseEvent,
    SseEventSink,
    encode_sse_event,
)

ROOT_GRAPH_RECURSION_LIMIT = 50
CHUNK_ENHANCEMENT_HISTORY_LIMIT = 4
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


@lru_cache
def get_embedding_model() -> Embeddings:
    """复用 Chunk 提交与历史召回使用的 Embedding Provider。"""
    return create_embedding_model()


@lru_cache
def get_semantic_enhancement_service() -> SemanticEnhancementService:
    """使用当前聊天模型配置复用 RAG 语义增强能力。"""
    settings = ModelSettings()
    return SemanticEnhancementService(
        create_chat_model(settings),
        model_name=settings.model_name,
    )


@lru_cache
def get_reranker() -> QwenTextReranker:
    """复用应用级千问 Reranker 及其 HTTP 连接池。"""
    return create_qwen_reranker(ModelSettings())


@lru_cache
def get_conversation_chunk_service() -> ConversationChunkService:
    """复用 Chunk Service，并使用与聊天模型相同的兼容接口配置。"""
    return ConversationChunkService(
        get_planning_repository(),
        get_embedding_model(),
        get_semantic_enhancement_service(),
    )


@lru_cache
def get_conversation_retrieval_service() -> ConversationRetrievalService:
    """复用限定当前用户和 Trip 的 Conversation 召回 Service。"""
    settings = ModelSettings()
    return ConversationRetrievalService(
        get_planning_repository(),
        get_embedding_model(),
        get_semantic_enhancement_service(),
        get_reranker(),
        candidate_limit=settings.rerank_candidate_limit,
        score_threshold=settings.rerank_score_threshold,
        dedup_similarity_threshold=settings.dedup_similarity_threshold,
    )


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
    reranker = get_reranker()
    try:
        await database.open()
        logger.info("PostgreSQL连接池已打开")
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
            retrieval_service = get_conversation_retrieval_service()
            history_tools = create_conversation_history_tools(retrieval_service)
            date_time_tools = create_date_time_tools()
            shared_query_tools = [
                *public_query_tools,
                *date_time_tools,
                *history_tools,
            ]
            # 根图、MCP 会话与 HTTP 客户端均由单一启动协程创建并在进程内复用。
            _app.state.root_graph = build_root_graph(
                create_chat_model(),
                repository,
                query_tools=shared_query_tools,
                retrieval_service=retrieval_service,
            )
            logger.info(
                "根图创建完成 shared_query_tools=%s",
                [tool.name for tool in shared_query_tools],
            )
            get_run_coordinator()
            logger.info("应用启动完成")
            try:
                yield
            finally:
                del _app.state.root_graph
    finally:
        try:
            await reranker.aclose()
        finally:
            get_run_coordinator.cache_clear()
            get_conversation_retrieval_service.cache_clear()
            get_reranker.cache_clear()
            get_conversation_chunk_service.cache_clear()
            get_semantic_enhancement_service.cache_clear()
            get_embedding_model.cache_clear()
            get_idempotency_repository.cache_clear()
            get_planning_repository.cache_clear()
            try:
                await database.close()
                logger.info("PostgreSQL连接池已关闭")
            finally:
                get_database.cache_clear()
                logger.info("应用关闭完成")
                shutdown_logging()


app = FastAPI(title="Tourism Agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """返回最小健康状态，供本地运行和后续部署检查使用。"""
    return {"status": "ok"}


async def load_conversation_page(
    repository: PlanningRepository,
    trip_id: UUID,
    *,
    before_message_id: int | None,
    limit: int,
) -> ConversationPage:
    """多取一条判断是否还有更早消息，并生成下一页游标。"""
    messages = await repository.get_conversation_page(
        trip_id,
        before_message_id=before_message_id,
        limit=limit + 1,
    )
    has_more = len(messages) > limit
    items = messages[-limit:]
    return ConversationPage(
        items=items,
        next_before_id=items[0].id if has_more else None,
        has_more=has_more,
    )


@app.get(
    "/trips/{trip_id}/bootstrap",
    response_model=TripBootstrapResponse,
)
async def get_trip_bootstrap(
    trip_id: UUID,
    user_id: Annotated[UUID, Query()],
    repository: Annotated[PlanningRepository, Depends(get_planning_repository)],
    message_limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> TripBootstrapResponse:
    """返回 Trip 页面首屏所需的最近对话与当前已确认行程。"""
    if not await repository.trip_belongs_to_user(user_id, trip_id):
        raise HTTPException(status_code=404, detail="未找到当前用户对应的旅行")
    conversations = await load_conversation_page(
        repository,
        trip_id,
        before_message_id=None,
        limit=message_limit,
    )
    return TripBootstrapResponse(
        trip_id=trip_id,
        conversations=conversations,
        current_itinerary=await repository.get_current_itinerary(trip_id),
    )


@app.get(
    "/trips/{trip_id}/conversations",
    response_model=ConversationPage,
)
async def get_trip_conversations(
    trip_id: UUID,
    user_id: Annotated[UUID, Query()],
    repository: Annotated[PlanningRepository, Depends(get_planning_repository)],
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> ConversationPage:
    """按消息 ID 游标读取当前 Trip 更早的原始对话。"""
    if not await repository.trip_belongs_to_user(user_id, trip_id):
        raise HTTPException(status_code=404, detail="未找到当前用户对应的旅行")
    return await load_conversation_page(
        repository,
        trip_id,
        before_message_id=before_id,
        limit=limit,
    )


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
    chunk_service: Annotated[
        ConversationChunkService,
        Depends(get_conversation_chunk_service),
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
    persisted_user_message: ConversationMessage | None = None

    async def persist_user_input() -> None:
        nonlocal persisted_user_message, user_message_id
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
        persisted_user_message = user_message
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
            "根图运行返回 trip_id=%s 最近Task=%s interrupted=%s",
            request.trip_id,
            result.get("route"),
            interrupted,
        )
        return await finalize_graph_response(
            graph=graph,
            thread_id=thread_id,
            request=request,
            result=result,
            repository=repository,
            idempotency_repository=idempotency_repository,
            chunk_service=chunk_service,
            persisted_user_message=cast(ConversationMessage, persisted_user_message),
        )

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


@app.post(
    "/messages/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "SSE 事件流，以 result 与 run.completed 收尾。",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        202: {
            "description": "相同幂等请求正在处理中，返回普通 JSON。",
            "model": IdempotencyProcessingResponse,
        },
        404: {"description": "用户作用域下不存在该旅行。"},
        409: {"description": "当前旅行正在处理，或幂等键被错误复用。"},
        422: {"description": "请求格式不合法，或候选确认值非法。"},
    },
)
async def handle_message_stream(
    request: MessageRequest,
    graph: Annotated[CompiledStateGraph, Depends(get_root_graph)],
    repository: Annotated[PlanningRepository, Depends(get_planning_repository)],
    idempotency_repository: Annotated[
        IdempotencyRepository,
        Depends(get_idempotency_repository),
    ],
    chunk_service: Annotated[
        ConversationChunkService,
        Depends(get_conversation_chunk_service),
    ],
    coordinator: Annotated[ThreadRunCoordinator, Depends(get_run_coordinator)],
) -> Response:
    """以 SSE 输出安全进度和最终结果，网络断开不取消后台图运行。"""
    logger.info(
        "API收到流式消息 user_id=%s trip_id=%s idempotency_id=%s message=%s",
        request.user_id,
        request.trip_id,
        request.idempotency_id,
        log_preview(request.message),
    )
    if not await repository.trip_belongs_to_user(request.user_id, request.trip_id):
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
    initial_snapshot = await graph.aget_state(config)
    try:
        validate_resume_message(initial_snapshot, request)
    except HTTPException as exc:
        await save_idempotent_error(
            idempotency_repository,
            request.idempotency_id,
            status=IdempotencyStatus.FAILED,
            response_status=exc.status_code,
            detail=exc.detail,
        )
        raise

    sink = SseEventSink()
    translator = GraphEventTranslator()
    sequence = 0
    persisted_user_message: ConversationMessage | None = None
    user_message_id = 0

    def emit(event: SseEvent) -> None:
        """补齐请求级字段后写入连接缓冲；断线后事件会直接丢弃。"""
        nonlocal sequence
        sequence += 1
        sink.emit(
            SseEvent(
                event=event.event,
                data={
                    "sequence": sequence,
                    "idempotency_id": str(request.idempotency_id),
                    "timestamp": datetime.now(UTC).isoformat(),
                    **event.data,
                },
            )
        )

    async def finish_stream_failure(error: BaseException) -> None:
        """无论失败发生在输入持久化还是图运行，都收束幂等与 SSE 终态。"""
        try:
            if isinstance(error, asyncio.CancelledError):
                logger.info("流式API运行已取消 trip_id=%s", request.trip_id)
                await save_idempotent_error(
                    idempotency_repository,
                    request.idempotency_id,
                    status=IdempotencyStatus.CANCELLED,
                    response_status=409,
                    detail="当前运行已取消",
                )
                emit(SseEvent(event="run.cancelled", data={"message": "本次运行已取消"}))
                emit(SseEvent(event="run.completed", data={"status": "cancelled"}))
            elif isinstance(error, HTTPException):
                await save_idempotent_error(
                    idempotency_repository,
                    request.idempotency_id,
                    status=IdempotencyStatus.FAILED,
                    response_status=error.status_code,
                    detail=error.detail,
                )
                emit(
                    SseEvent(
                        event="error",
                        data={
                            "code": "REQUEST_ERROR",
                            "message": str(error.detail),
                            "retryable": False,
                        },
                    )
                )
                emit(SseEvent(event="run.completed", data={"status": "failed"}))
            else:
                logger.exception(
                    "流式API消息处理异常 trip_id=%s idempotency_id=%s",
                    request.trip_id,
                    request.idempotency_id,
                    exc_info=error,
                )
                body = {"detail": "消息处理失败，请稍后使用新的 idempotency_id 重试"}
                await idempotency_repository.finish(
                    request.idempotency_id,
                    status=IdempotencyStatus.FAILED,
                    response_status=500,
                    response_body=body,
                )
                emit(
                    SseEvent(
                        event="error",
                        data={
                            "code": "MODEL_ERROR",
                            "message": "消息处理失败，请稍后重试",
                            "retryable": True,
                        },
                    )
                )
                emit(SseEvent(event="run.completed", data={"status": "failed"}))
        finally:
            sink.close()

    async def persist_user_input() -> None:
        nonlocal persisted_user_message, user_message_id
        snapshot = await graph.aget_state(config)
        validate_resume_message(snapshot, request)
        persisted_user_message = await repository.append_conversation(
            request.trip_id,
            ConversationRole.USER,
            request.message,
        )
        user_message_id = persisted_user_message.id
        logger.info(
            "流式用户消息已写入Conversation trip_id=%s user_message_id=%s",
            request.trip_id,
            user_message_id,
        )

    async def run_graph_stream() -> None:
        emit(
            SseEvent(
                event="run.started",
                data={
                    "trip_id": str(request.trip_id),
                    "message": "正在处理你的请求",
                },
            )
        )
        snapshot = await graph.aget_state(config)
        graph_input: dict[str, object] | Command
        if snapshot.interrupts:
            logger.info("流式接口恢复等待中的根图 trip_id=%s", request.trip_id)
            graph_input = Command(resume=request.message)
        else:
            logger.info("流式接口启动新的根图运行 trip_id=%s", request.trip_id)
            graph_input = {
                "user_id": request.user_id,
                "trip_id": request.trip_id,
                "user_message_id": user_message_id,
                "user_input": request.message,
            }

        async for graph_event in graph.astream_events(
            graph_input,
            config,
            version="v2",
        ):
            for stream_event in translator.translate(graph_event):
                emit(stream_event)

        result = graph_state_result(await graph.aget_state(config))
        interrupted = bool(result.get("__interrupt__"))
        logger.info(
            "流式根图运行返回 trip_id=%s 最近Task=%s interrupted=%s",
            request.trip_id,
            result.get("route"),
            interrupted,
        )
        response = await finalize_graph_response(
            graph=graph,
            thread_id=thread_id,
            request=request,
            result=result,
            repository=repository,
            idempotency_repository=idempotency_repository,
            chunk_service=chunk_service,
            persisted_user_message=cast(ConversationMessage, persisted_user_message),
        )
        if interrupted:
            emit(SseEvent(event="interaction.required", data=interrupt_event_data(result)))
        emit(SseEvent(event="result", data=response.model_dump(mode="json")))
        emit(
            SseEvent(
                event="run.completed",
                data={"status": "waiting_user" if interrupted else "completed"},
            )
        )
        sink.close()

    try:
        await coordinator.start(
            thread_id,
            run_graph_stream,
            before_cancellation=persist_user_input,
            on_failure=finish_stream_failure,
        )
    except ThreadBusyError as exc:
        await save_idempotent_error(
            idempotency_repository,
            request.idempotency_id,
            status=IdempotencyStatus.FAILED,
            response_status=409,
            detail="当前旅行正在处理中，请先取消",
        )
        raise HTTPException(status_code=409, detail="当前旅行正在处理中，请先取消") from exc

    async def event_stream() -> AsyncIterator[bytes]:
        """消费生产者队列；客户端退出时不会取消已经登记的后台任务。"""
        try:
            while (event := await sink.next_event()) is not None:
                yield encode_sse_event(event)
        finally:
            sink.detach()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


async def finalize_graph_response(
    *,
    graph: CompiledStateGraph,
    thread_id: str,
    request: MessageRequest,
    result: dict[str, object],
    repository: PlanningRepository,
    idempotency_repository: IdempotencyRepository,
    chunk_service: ConversationChunkService,
    persisted_user_message: ConversationMessage,
) -> MessageResponse:
    """持久化一次用户可见响应，并完成幂等记录与 checkpoint 收尾。"""
    interrupted = bool(result.get("__interrupt__"))
    response_committed = False
    try:
        response_message = get_user_visible_message(result)
        assistant_message = await repository.append_conversation(
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
        try:
            recent_conversation = await repository.get_recent_conversation(
                request.trip_id,
                before_message_id=persisted_user_message.id,
                limit=CHUNK_ENHANCEMENT_HISTORY_LIMIT,
            )
            context_goal = (
                cast(TaskSpec, result["current_task"]).instruction
                if interrupted
                else str(result["orchestration_goal"])
            )
            await chunk_service.submit(
                trip_id=request.trip_id,
                exchange_id=request.idempotency_id,
                user_message=persisted_user_message,
                assistant_message=assistant_message,
                context_goal=context_goal,
                recent_conversation=recent_conversation,
            )
            logger.info(
                "Conversation Chunk提交完成 trip_id=%s exchange_id=%s",
                request.trip_id,
                request.idempotency_id,
            )
        except Exception:
            # Chunk 是可重建派生数据，索引失败不能破坏已经生成的用户响应。
            logger.exception(
                "Conversation Chunk提交失败 trip_id=%s exchange_id=%s",
                request.trip_id,
                request.idempotency_id,
            )
    finally:
        # 用户未看到提问时不能保留可恢复的 interrupt，否则重试会被误当作回答。
        if interrupted and not response_committed:
            logger.warning("响应写入失败，清除interrupt checkpoint trip_id=%s", request.trip_id)
            await graph.checkpointer.adelete_thread(thread_id)

    response = MessageResponse(
        route=cast(TaskType, result["route"]),
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
        "API消息处理完成 trip_id=%s 最近Task=%s has_candidate=%s has_current_itinerary=%s",
        request.trip_id,
        response.route.value,
        response.candidate_itinerary is not None,
        response.current_itinerary is not None,
    )
    return response


def graph_state_result(snapshot: object) -> dict[str, object]:
    """把 checkpoint 快照转换为与 ainvoke 返回值一致的最小结果。"""
    values = dict(cast(Any, snapshot).values)
    interrupts = cast(Any, snapshot).interrupts
    if interrupts:
        values["__interrupt__"] = interrupts
    return values


def validate_resume_message(snapshot: object, request: MessageRequest) -> None:
    """候选行程确认只允许固定回答，其他 interrupt 仍接受自由输入。"""
    interrupts = cast(Any, snapshot).interrupts
    if not interrupts:
        return
    interrupt_value = interrupts[0].value
    if (
        interrupt_value.get("kind") == "candidate_confirmation"
        and request.message not in {"是", "否"}
    ):
        logger.warning(
            "API拒绝无效候选确认 trip_id=%s message=%s",
            request.trip_id,
            log_preview(request.message),
        )
        raise HTTPException(status_code=422, detail="候选方案确认只接受“是”或“否”")


def interrupt_event_data(result: dict[str, object]) -> dict[str, object]:
    """将 Graph interrupt 转为前端交互控件可直接消费的最小载荷。"""
    interrupt = result["__interrupt__"][0].value
    kind = str(interrupt["kind"])
    return {
        "kind": kind,
        "question": str(interrupt["question"]),
        "allowed_answers": ["是", "否"] if kind == "candidate_confirmation" else None,
        "candidate_itinerary": interrupt.get("candidate_itinerary"),
    }


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
