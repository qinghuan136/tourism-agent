"""集中管理聊天模型配置并创建模型实例。"""

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelSettings(BaseSettings):
    """从项目根目录的 .env 或进程环境读取模型配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    model_name: str = Field(
        default="gpt-4.1-mini",
        validation_alias="TOURISM_AGENT_MODEL",
    )
    api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    base_url: str | None = Field(default=None, validation_alias="OPENAI_BASE_URL")
    run_llm_integration: bool = Field(
        default=False,
        validation_alias="RUN_LLM_INTEGRATION",
    )


def create_chat_model(settings: ModelSettings | None = None) -> BaseChatModel:
    """创建 OpenAI 兼容模型，并显式传递项目模型配置。"""
    settings = settings or ModelSettings()
    provider_options: dict[str, str] = {}

    if settings.api_key is not None:
        provider_options["api_key"] = settings.api_key.get_secret_value()
    if settings.base_url is not None:
        provider_options["base_url"] = settings.base_url

    return init_chat_model(
        settings.model_name,
        model_provider="openai",
        **provider_options,
    )
