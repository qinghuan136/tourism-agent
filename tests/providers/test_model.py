"""验证聊天模型配置与创建参数。"""

from importlib import import_module


def test_model_settings_load_environment_variables(monkeypatch) -> None:
    """模型名称、密钥、Base URL 和测试开关都应由环境变量控制。"""
    monkeypatch.setenv("TOURISM_AGENT_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("RUN_LLM_INTEGRATION", "true")

    model_module = import_module("tourism_agent.providers.model")
    settings = model_module.ModelSettings(_env_file=None)

    assert settings.model_name == "test-model"
    assert settings.api_key.get_secret_value() == "test-api-key"
    assert settings.base_url == "https://model.example/v1"
    assert settings.run_llm_integration is True


def test_create_chat_model_passes_explicit_provider_configuration(monkeypatch) -> None:
    """模型工厂应把配置显式传给 LangChain，而不是依赖隐式 SDK 行为。"""
    model_module = import_module("tourism_agent.providers.model")
    captured: dict[str, object] = {}
    sentinel_model = object()

    def fake_init_chat_model(model: str, **kwargs: object) -> object:
        captured["model"] = model
        captured["kwargs"] = kwargs
        return sentinel_model

    monkeypatch.setattr(model_module, "init_chat_model", fake_init_chat_model)
    settings = model_module.ModelSettings(
        model_name="test-model",
        api_key="test-api-key",
        base_url="https://model.example/v1",
        run_llm_integration=False,
        _env_file=None,
    )

    result = model_module.create_chat_model(settings)

    assert result is sentinel_model
    assert captured == {
        "model": "test-model",
        "kwargs": {
            "model_provider": "openai",
            "api_key": "test-api-key",
            "base_url": "https://model.example/v1",
        },
    }
