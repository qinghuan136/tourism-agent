"""验证外部查询客户端与根图共享同一个应用生命周期。"""

import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache
from types import SimpleNamespace

import pytest


class FakeDatabase:
    """记录数据库连接池的开启和关闭。"""

    def __init__(self) -> None:
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True


class FakeReranker:
    """记录应用退出时是否释放 Reranker HTTP 资源。"""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_lifespan_closes_resources_when_database_open_fails(monkeypatch) -> None:
    """数据库启动失败时，已创建的 Reranker 和数据库池仍必须关闭。"""
    from tourism_agent import api

    class FailingDatabase(FakeDatabase):
        async def open(self) -> None:
            raise RuntimeError("数据库启动失败")

    database = FailingDatabase()
    reranker = FakeReranker()
    logging_shutdown = False

    def fake_shutdown_logging() -> None:
        nonlocal logging_shutdown
        logging_shutdown = True

    monkeypatch.setattr(api, "get_database", lru_cache(lambda: database))
    monkeypatch.setattr(api, "get_reranker", lru_cache(lambda: reranker))
    monkeypatch.setattr(api, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(api, "shutdown_logging", fake_shutdown_logging)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="数据库启动失败"):
            async with api.lifespan(SimpleNamespace(state=SimpleNamespace())):
                raise AssertionError("数据库启动失败后不应进入应用运行阶段")

    asyncio.run(scenario())

    assert database.closed
    assert reranker.closed
    assert logging_shutdown


def test_lifespan_closes_database_when_reranker_close_fails(monkeypatch) -> None:
    """Reranker 关闭异常不得阻断数据库池和日志清理。"""
    from tourism_agent import api

    class FailingCloseReranker(FakeReranker):
        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("Reranker关闭失败")

    database = FakeDatabase()
    reranker = FailingCloseReranker()
    logging_shutdown = False

    def fail_settings() -> object:
        raise RuntimeError("模拟后续启动失败")

    def fake_shutdown_logging() -> None:
        nonlocal logging_shutdown
        logging_shutdown = True

    monkeypatch.setattr(api, "get_database", lru_cache(lambda: database))
    monkeypatch.setattr(api, "get_reranker", lru_cache(lambda: reranker))
    monkeypatch.setattr(api, "get_planning_repository", lru_cache(lambda: object()))
    monkeypatch.setattr(api, "TravelToolSettings", fail_settings)
    monkeypatch.setattr(api, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(api, "shutdown_logging", fake_shutdown_logging)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="Reranker关闭失败"):
            async with api.lifespan(SimpleNamespace(state=SimpleNamespace())):
                raise AssertionError("启动失败后不应进入应用运行阶段")

    asyncio.run(scenario())

    assert database.closed
    assert reranker.closed
    assert logging_shutdown


def test_lifespan_injects_shared_query_tools_into_root_graph(monkeypatch) -> None:
    """应用启动时创建查询客户端，退出时释放并移除根图引用。"""
    from tourism_agent import api

    database = FakeDatabase()
    repository = object()
    model = object()
    settings = object()
    query_clients = SimpleNamespace(
        weather=object(),
        places=object(),
        routes=object(),
        web_search=object(),
    )
    query_tools = [
        SimpleNamespace(name="get_weather"),
        SimpleNamespace(name="search_places"),
        SimpleNamespace(name="get_place_details"),
        SimpleNamespace(name="search_nearby_places"),
        SimpleNamespace(name="web_search"),
        SimpleNamespace(name="extract_web_content"),
        SimpleNamespace(name="plan_route"),
        SimpleNamespace(name="measure_travel_distance"),
    ]
    retrieval_service = object()
    reranker = FakeReranker()
    history_tools = [
        SimpleNamespace(name="search_conversation_history"),
        SimpleNamespace(name="read_conversation_exchanges"),
    ]
    date_time_tools = [
        SimpleNamespace(name="get_current_datetime"),
        SimpleNamespace(name="calculate_date"),
        SimpleNamespace(name="calculate_trip_duration"),
    ]
    graph = object()
    app = SimpleNamespace(state=SimpleNamespace())
    lifecycle_closed = False
    graph_arguments: dict[str, object] = {}

    @asynccontextmanager
    async def fake_open_clients(received_settings: object):
        nonlocal lifecycle_closed
        assert received_settings is settings
        try:
            yield query_clients
        finally:
            lifecycle_closed = True

    def fake_build_root_graph(
        received_model: object,
        received_repository: object,
        *,
        query_tools: list[object],
        retrieval_service: object,
    ) -> object:
        graph_arguments.update(
            model=received_model,
            repository=received_repository,
            query_tools=query_tools,
            retrieval_service=retrieval_service,
        )
        return graph

    monkeypatch.setattr(api, "get_database", lru_cache(lambda: database))
    monkeypatch.setattr(api, "get_planning_repository", lru_cache(lambda: repository))
    monkeypatch.setattr(api, "create_chat_model", lambda: model)
    monkeypatch.setattr(api, "TravelToolSettings", lambda: settings)
    monkeypatch.setattr(api, "open_travel_query_clients", fake_open_clients)
    monkeypatch.setattr(api, "create_query_tools", lambda *_clients: query_tools)
    monkeypatch.setattr(api, "create_date_time_tools", lambda: date_time_tools)
    monkeypatch.setattr(
        api,
        "get_conversation_retrieval_service",
        lru_cache(lambda: retrieval_service),
    )
    monkeypatch.setattr(api, "get_reranker", lru_cache(lambda: reranker))
    monkeypatch.setattr(
        api,
        "create_conversation_history_tools",
        lambda received_service: (
            history_tools if received_service is retrieval_service else []
        ),
    )
    monkeypatch.setattr(api, "build_root_graph", fake_build_root_graph)
    monkeypatch.setattr(api, "get_run_coordinator", lru_cache(lambda: object()))
    monkeypatch.setattr(api, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(api, "shutdown_logging", lambda: None)

    async def run_lifespan() -> None:
        async with api.lifespan(app):
            assert database.opened
            assert app.state.root_graph is graph
            assert graph_arguments == {
                "model": model,
                "repository": repository,
                "query_tools": [*query_tools, *date_time_tools, *history_tools],
                "retrieval_service": retrieval_service,
            }

        assert database.closed
        assert lifecycle_closed
        assert reranker.closed
        assert not hasattr(app.state, "root_graph")

    asyncio.run(run_lifespan())


def test_retrieval_service_wiring_uses_shared_reranker_and_tuning(monkeypatch) -> None:
    """应用级召回 Service 应复用 Reranker，并接收可调检索参数。"""
    from tourism_agent import api

    repository = object()
    embeddings = object()
    enhancer = object()
    reranker = object()
    sentinel_service = object()
    captured: dict[str, object] = {}
    settings = SimpleNamespace(
        rerank_candidate_limit=24,
        rerank_score_threshold=0.35,
        dedup_similarity_threshold=0.96,
    )

    def fake_service(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel_service

    monkeypatch.setattr(api, "get_planning_repository", lambda: repository)
    monkeypatch.setattr(api, "get_embedding_model", lambda: embeddings)
    monkeypatch.setattr(api, "get_semantic_enhancement_service", lambda: enhancer)
    monkeypatch.setattr(api, "get_reranker", lambda: reranker)
    monkeypatch.setattr(api, "ModelSettings", lambda: settings)
    monkeypatch.setattr(api, "ConversationRetrievalService", fake_service)
    api.get_conversation_retrieval_service.cache_clear()

    result = api.get_conversation_retrieval_service()

    assert result is sentinel_service
    assert captured == {
        "args": (repository, embeddings, enhancer, reranker),
        "kwargs": {
            "candidate_limit": 24,
            "score_threshold": 0.35,
            "dedup_similarity_threshold": 0.96,
        },
    }
    api.get_conversation_retrieval_service.cache_clear()
