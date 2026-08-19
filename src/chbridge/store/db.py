"""asyncpg 커넥션 풀 래퍼.

ORM 을 쓰지 않는다. 핫패스가 MessageLink 단건 조회/삽입이고 SQL 이 단순해서
추상화 계층이 이득보다 비용이 크다. (실행계획서 2.1)

mypy strict 대응: asyncpg 에 타입 스텁이 없어 반환값이 Any 다.
경계에서 cast 로 좁혀 상위 계층에 Any 가 새지 않게 한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import asyncpg
import structlog

log = structlog.get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class Database:
    """커넥션 풀 소유자. 애플리케이션 수명과 같이 간다."""

    def __init__(self, dsn: str, *, min_size: int = 2, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=30,
        )
        log.info("db.connected", min_size=self._min_size, max_size=self._max_size)

    async def close(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None
        log.info("db.closed")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() 가 먼저 호출되어야 합니다")
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        async with self.pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        async with self.pool.acquire() as conn, conn.transaction():
            yield conn

    # ------------------------------------------------------------------ 헬퍼

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.acquire() as conn:
            return cast(str, await conn.execute(sql, *args))

    async def fetch(self, sql: str, *args: Any) -> Sequence[Any]:
        async with self.acquire() as conn:
            return cast(Sequence[Any], await conn.fetch(sql, *args))

    async def fetchrow(self, sql: str, *args: Any) -> Any | None:
        async with self.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(sql, *args)


async def apply_migrations(db: Database, *, directory: Path | None = None) -> list[str]:
    """번호순 SQL 파일을 순서대로 적용한다.

    ORM 이 없으므로 Alembic 은 부적합하다. 파일명 앞의 번호가 적용 순서이며,
    적용된 파일명을 schema_migrations 에 기록해 재실행을 막는다.
    각 파일은 하나의 트랜잭션으로 적용된다 — 중간 실패 시 부분 적용이 남지 않는다.
    """
    directory = directory or MIGRATIONS_DIR
    files = sorted(directory.glob("[0-9]*.sql"))
    if not files:
        log.warning("migrations.none_found", directory=str(directory))
        return []

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    rows = await db.fetch("SELECT filename FROM schema_migrations")
    applied: set[str] = {str(r["filename"]) for r in rows}

    newly: list[str] = []
    for path in files:
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        async with db.transaction() as conn:
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", path.name)
        newly.append(path.name)
        log.info("migrations.applied", filename=path.name)

    if not newly:
        log.info("migrations.up_to_date", count=len(applied))
    return newly
