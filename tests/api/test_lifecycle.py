"""验证外部查询客户端与根图共享同一个应用生命周期。"""

import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache
from types import SimpleNamespace


class FakeDatabase:
    """记录数据库连接池的开启和关闭。"""

    def __init__(self) -> None:
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True


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
    ) -> object:
        graph_arguments.update(
            model=received_model,
            repository=received_repository,
            query_tools=query_tools,
        )
        return graph

    monkeypatch.setattr(api, "get_database", lru_cache(lambda: database))
    monkeypatch.setattr(api, "get_planning_repository", lru_cache(lambda: repository))
    monkeypatch.setattr(api, "create_chat_model", lambda: model)
    monkeypatch.setattr(api, "TravelToolSettings", lambda: settings)
    monkeypatch.setattr(api, "open_travel_query_clients", fake_open_clients)
    monkeypatch.setattr(api, "create_query_tools", lambda *_clients: query_tools)
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
                "query_tools": query_tools,
            }

        assert database.closed
        assert lifecycle_closed
        assert not hasattr(app.state, "root_graph")

    asyncio.run(run_lifespan())
