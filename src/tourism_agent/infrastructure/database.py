"""提供 PostgreSQL 配置和异步连接池。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """从项目根目录的 .env 或进程环境读取 PostgreSQL 配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    port: int = Field(default=5432, validation_alias="POSTGRES_PORT")
    database: str = Field(validation_alias="POSTGRES_DB")
    user: str = Field(validation_alias="POSTGRES_USER")
    password: SecretStr = Field(validation_alias="POSTGRES_PASSWORD")

    def connection_parameters(self) -> dict[str, str | int]:
        """生成 psycopg 参数，避免手工拼接包含特殊字符的连接 URL。"""
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password.get_secret_value(),
        }


class PostgresDatabase:
    """管理应用共享的 PostgreSQL 异步连接池。"""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._pool = AsyncConnectionPool(
            conninfo="",
            kwargs={
                **settings.connection_parameters(),
                "row_factory": dict_row,
            },
            min_size=1,
            max_size=5,
            open=False,
        )

    async def open(self) -> None:
        """打开连接池并等待首批连接建立。"""
        await self._pool.open(wait=True)

    async def close(self) -> None:
        """关闭连接池并释放数据库连接。"""
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
        """向 Repository 或迁移程序提供一个自动归还的连接。"""
        async with self._pool.connection() as connection:
            yield connection
