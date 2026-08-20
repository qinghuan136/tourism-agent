"""配置 Tourism Agent 的控制台与轮转文件日志。"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LOGGER_NAME = "tourism_agent"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class LoggingSettings(BaseSettings):
    """从环境变量读取项目日志级别与文件位置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    log_level: str = Field(default="INFO", validation_alias="TOURISM_LOG_LEVEL")
    log_file: Path = Field(
        default=Path("logs/tourism-agent.log"),
        validation_alias="TOURISM_LOG_FILE",
    )


def configure_logging(settings: LoggingSettings | None = None) -> None:
    """为项目 Logger 配置控制台和 UTF-8 轮转文件输出。"""
    settings = settings or LoggingSettings()
    logger = logging.getLogger(LOGGER_NAME)
    close_handlers(logger)

    level = getattr(logging, settings.log_level.upper())
    formatter = logging.Formatter(LOG_FORMAT)
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)


def shutdown_logging() -> None:
    """关闭项目日志句柄，确保文件缓冲区完整写入。"""
    logger = logging.getLogger(LOGGER_NAME)
    close_handlers(logger)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


def close_handlers(logger: logging.Logger) -> None:
    """移除并关闭 Logger 当前持有的输出句柄。"""
    for handler in logger.handlers[:]:
        handler.flush()
        handler.close()
        logger.removeHandler(handler)


def log_preview(value: object, *, limit: int = 2000) -> str:
    """把可能较长的运行内容压缩成适合日志记录的单行预览。"""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[:limit]}…"
