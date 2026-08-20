"""验证项目日志同时写入控制台和轮转文件。"""

import logging
from importlib import import_module
from pathlib import Path


def test_configure_logging_writes_console_and_file(capsys) -> None:
    """重要运行日志应同时出现在控制台和指定日志文件。"""
    logging_module = import_module("tourism_agent.infrastructure.logging_config")
    log_file = Path(".tmp/test-tourism-agent.log")
    log_file.parent.mkdir(exist_ok=True)
    log_file.unlink(missing_ok=True)
    settings = logging_module.LoggingSettings(
        log_level="INFO",
        log_file=log_file,
    )

    logging_module.configure_logging(settings)
    try:
        logging.getLogger("tourism_agent.test").info("根图节点进入 trip_id=test-trip")
        console_output = capsys.readouterr().out
    finally:
        logging_module.shutdown_logging()

    try:
        file_output = log_file.read_text(encoding="utf-8")
        assert "根图节点进入 trip_id=test-trip" in console_output
        assert "根图节点进入 trip_id=test-trip" in file_output
    finally:
        log_file.unlink(missing_ok=True)


def test_log_preview_limits_large_or_multiline_content() -> None:
    """用户和 Tool 内容写日志前应压成单行并限制长度。"""
    logging_module = import_module("tourism_agent.infrastructure.logging_config")

    result = logging_module.log_preview("第一行\n第二行内容很长", limit=8)

    assert result == "第一行 第二行内…"
