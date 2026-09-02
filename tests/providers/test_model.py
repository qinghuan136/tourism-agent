"""验证聊天模型配置与创建参数。"""

from importlib import import_module


def test_model_settings_load_environment_variables(monkeypatch) -> None:
    """模型名称、密钥、Base URL 和测试开关都应由环境变量控制。"""
    monkeypatch.setenv("TOURISM_AGENT_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("TOURISM_AGENT_RERANK_URL", "https://rerank.example/rerank")
    monkeypatch.setenv("RAG_RERANK_SCORE_THRESHOLD", "0.35")
    monkeypatch.setenv("RAG_DEDUP_SIMILARITY_THRESHOLD", "0.96")
    monkeypatch.setenv("RAG_CANDIDATE_LIMIT", "24")
    monkeypatch.setenv("RUN_LLM_INTEGRATION", "true")

    model_module = import_module("tourism_agent.providers.model")
    settings = model_module.ModelSettings(_env_file=None)

    assert settings.model_name == "test-model"
    assert settings.api_key.get_secret_value() == "test-api-key"
    assert settings.base_url == "https://model.example/v1"
    assert settings.rerank_url == "https://rerank.example/rerank"
    assert settings.rerank_score_threshold == 0.35
    assert settings.dedup_similarity_threshold == 0.96
    assert settings.rerank_candidate_limit == 24
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


def test_create_embedding_model_uses_fixed_model_and_dimensions(monkeypatch) -> None:
    """Embedding 工厂必须复用兼容接口配置并固定模型与 1024 维。"""
    model_module = import_module("tourism_agent.providers.model")
    captured: dict[str, object] = {}
    sentinel_embeddings = object()

    def fake_openai_embeddings(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel_embeddings

    monkeypatch.setattr(model_module, "OpenAIEmbeddings", fake_openai_embeddings)
    settings = model_module.ModelSettings(
        model_name="test-chat-model",
        api_key="test-api-key",
        base_url="https://model.example/v1",
        _env_file=None,
    )

    result = model_module.create_embedding_model(settings)

    assert result is sentinel_embeddings
    assert captured == {
        "model": "qwen3.7-text-embedding",
        "dimensions": 1024,
        "api_key": "test-api-key",
        "base_url": "https://model.example/v1",
        "check_embedding_ctx_length": False,
    }
