"""提供不依赖外部服务的公共日期时间 Tools。"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.tools import BaseTool, tool

logger = logging.getLogger(__name__)
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
WEEKDAY_NAMES = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)


def _parse_date(value: str, field_name: str) -> date:
    """读取 Tool 输入中的 ISO 日期，并返回便于模型修正的错误。"""
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name}必须使用 YYYY-MM-DD 格式") from error


@tool
def get_current_datetime() -> dict[str, str]:
    """获取中国标准时间下的当前日期、具体时间和星期。"""
    current = datetime.now(SHANGHAI_TIMEZONE).replace(microsecond=0)
    result = {
        "timezone": "Asia/Shanghai",
        "datetime": current.isoformat(),
        "date": current.date().isoformat(),
        "time": current.time().isoformat(),
        "weekday": WEEKDAY_NAMES[current.weekday()],
    }
    logger.info("Tool调用完成 name=get_current_datetime datetime=%s", result["datetime"])
    return result


@tool
def calculate_date(base_date: str, offset_days: int) -> dict[str, str]:
    """按天数偏移计算目标日期；base_date 必须使用 YYYY-MM-DD 格式。"""
    target = _parse_date(base_date, "基准日期") + timedelta(days=offset_days)
    result = {
        "date": target.isoformat(),
        "weekday": WEEKDAY_NAMES[target.weekday()],
    }
    logger.info(
        "Tool调用完成 name=calculate_date base_date=%s offset_days=%d target=%s",
        base_date,
        offset_days,
        result["date"],
    )
    return result


@tool
def calculate_trip_duration(start_date: str, end_date: str) -> dict[str, int]:
    """计算含首尾日期的旅行自然日数，以及两个日期之间的住宿晚数。"""
    start = _parse_date(start_date, "开始日期")
    end = _parse_date(end_date, "结束日期")
    nights = (end - start).days
    if nights < 0:
        raise ValueError("结束日期不能早于开始日期")
    result = {"calendar_days": nights + 1, "nights": nights}
    logger.info(
        "Tool调用完成 name=calculate_trip_duration start_date=%s end_date=%s "
        "calendar_days=%d nights=%d",
        start_date,
        end_date,
        result["calendar_days"],
        result["nights"],
    )
    return result


def create_date_time_tools() -> list[BaseTool]:
    """返回可供各业务子图按白名单选取的日期时间 Tools。"""
    return [get_current_datetime, calculate_date, calculate_trip_duration]
