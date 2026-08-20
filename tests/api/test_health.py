"""验证应用最小运行入口。"""

from importlib import import_module

from fastapi.testclient import TestClient


def test_health_endpoint_reports_service_is_running() -> None:
    """健康检查应以稳定响应证明 FastAPI 应用已经启动。"""
    api = import_module("tourism_agent.api")
    client = TestClient(api.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
