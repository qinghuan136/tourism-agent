"""提供兼容 Windows Psycopg 异步连接池的统一服务器入口。"""

import asyncio
import sys

import uvicorn


def main() -> None:
    """启动 FastAPI；Windows 显式避开 Psycopg 不支持的 ProactorEventLoop。"""
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else "auto"
    uvicorn.run("tourism_agent.api:app", loop=loop_factory)


if __name__ == "__main__":
    main()
