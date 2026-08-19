"""재연결 백필 커서. (PRD 5.9 / FR-8)

Mattermost WebSocket 과 Slack Socket Mode 는 **연결이 끊긴 동안의 이벤트를
재생해주지 않는다.** 따라서 재연결 시 마지막 처리 지점부터 직접 긁어와야 한다.

이 커서 없이 백필을 생략하면 조용한 메시지 유실이 발생한다 — 로그에도 남지
않아서 발견이 매우 늦다. Phase 1 필수 항목인 이유다.

커서 값은 플랫폼별 의미가 다르므로 문자열로 보관한다.
  Mattermost : epoch milliseconds  -> GET /channels/{id}/posts?since=
  Slack      : ts (예 "1786107692.046269") -> conversations.history(oldest=)
"""

from __future__ import annotations

import structlog

from chbridge.store.db import Database

log = structlog.get_logger(__name__)


class CursorStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, endpoint_id: str) -> str | None:
        value = await self._db.fetchval(
            "SELECT cursor_value FROM sync_cursors WHERE endpoint_id = $1", endpoint_id
        )
        return None if value is None else str(value)

    async def set(self, endpoint_id: str, cursor_value: str) -> None:
        await self._db.execute(
            """
            INSERT INTO sync_cursors (endpoint_id, cursor_value)
            VALUES ($1, $2)
            ON CONFLICT (endpoint_id)
                DO UPDATE SET cursor_value = EXCLUDED.cursor_value, updated_at = now()
            """,
            endpoint_id,
            cursor_value,
        )

    async def advance(self, endpoint_id: str, cursor_value: str) -> None:
        """커서를 전진시킨다. 뒤로 가는 값은 무시한다.

        이벤트가 순서를 바꿔 도착하더라도 커서가 뒤로 밀리면 안 된다.
        뒤로 밀리면 재연결 시 이미 처리한 구간을 다시 긁어오게 되고,
        event_inbox 의 중복 차단에 불필요한 부하를 준다.

        문자열 비교로는 정렬이 어긋날 수 있어 수치 비교를 시도하고,
        실패하면 문자열 비교로 낙착한다.
        """
        current = await self.get(endpoint_id)
        if current is not None and not _is_newer(cursor_value, current):
            return
        await self.set(endpoint_id, cursor_value)


def _is_newer(candidate: str, current: str) -> bool:
    try:
        return float(candidate) > float(current)
    except ValueError:
        return candidate > current
