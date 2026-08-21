"""验证跨平台服务器入口选择与 Psycopg 兼容的事件循环。"""

import asyncio
from importlib import import_module
from typing import Any

import pytest


def load_server_module() -> Any:
    """把入口缺失转换为明确的行为失败，便于 TDD 展示预期。"""
    try:
        return import_module("tourism_agent.server")
    except ModuleNotFoundError:
        pytest.fail("缺少统一服务器入口 tourism_agent.server")


def capture_run_config(monkeypatch: pytest.MonkeyPatch, platform: str) -> dict[str, Any]:
    """拦截真正的网络监听，仅检查传给 Uvicorn 的启动契约。"""
    server = load_server_module()
    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured.update({"app": app, **kwargs})

    monkeypatch.setattr(server.sys, "platform", platform)
    monkeypatch.setattr(server.uvicorn, "run", fake_run)
    server.main()
    return captured


def test_server_uses_selector_event_loop_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 若退回 ProactorEventLoop，Psycopg 异步连接池将无法启动。"""
    config = capture_run_config(monkeypatch, "win32")

    assert config["app"] == "tourism_agent.api:app"
    assert config["loop"] is asyncio.SelectorEventLoop


def test_server_keeps_uvicorn_auto_loop_on_other_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 Windows 平台不应被强制使用 Windows 专用兼容策略。"""
    config = capture_run_config(monkeypatch, "linux")

    assert config["app"] == "tourism_agent.api:app"
    assert config["loop"] == "auto"
