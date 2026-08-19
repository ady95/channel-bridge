"""영속 이벤트 큐 + 멱등 처리. (PRD 5.9)

수신 이벤트를 **먼저 DB에 기록하고** 처리한다. 이것이 NFR-2 의
"재시작·단절 후 메시지 유실 0건"을 보장하는 지점이다. 인메모리 큐만 쓰면
프로세스가 죽는 순간 미처리 이벤트가 사라진다.

멱등성은 UNIQUE (platform, workspace_id, event_key) 제약이 담당한다.
이 하나로 두 가지를 동시에 막는다.
  - 플랫폼의 이벤트 재전송 (Slack Socket Mode 는 재전송이 있다)
  - 재연결 백필과 정상 스트림이 겹치는 구간 (FR-8.3)
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from chbridge.cir import Event
from chbridge.store.db import Database

log = structlog.get_logger(__name__)


class InboxItem:
    __slots__ = ("attempts", "bridge_id", "event", "row_id")

    def __init__(self, *, row_id: int, event: Event, bridge_id: str | None, attempts: int) -> None:
        self.row_id = row_id
        self.event = event
        self.bridge_id = bridge_id
        self.attempts = attempts

    def __repr__(self) -> str:
        return f"<InboxItem #{self.row_id} {self.event.kind} attempts={self.attempts}>"


class InboxStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def enqueue(self, event: Event, *, bridge_id: str | None) -> bool:
        """이벤트를 큐에 넣는다.

        Returns:
            True  최초 수신이라 큐에 들어감
            False 이미 본 이벤트라 폐기됨 (재전송 / 백필 중복)
        """
        row_id = await self._db.fetchval(
            """
            INSERT INTO event_inbox
                (platform, workspace_id, event_key, channel_id, bridge_id, payload)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (platform, workspace_id, event_key) DO NOTHING
            RETURNING id
            """,
            event.source.platform.value,
            event.source.workspace_id,
            event.event_key,
            event.source.channel_id,
            bridge_id,
            json.dumps(event.to_json()),
        )
        if row_id is None:
            log.debug(
                "inbox.duplicate_dropped",
                event_key=event.event_key,
                kind=event.kind.value,
            )
            return False
        return True

    async def claim(self, channel_id: str) -> InboxItem | None:
        """해당 채널의 가장 오래된 pending 이벤트를 하나 집는다.

        채널당 워커가 하나뿐이므로 FIFO 가 보장된다. (PRD 5.9)
        SKIP LOCKED 는 다중 프로세스 확장 시를 위한 예비 조치다.
        """
        row = await self._db.fetchrow(
            """
            UPDATE event_inbox
            SET status = 'processing', attempts = attempts + 1
            WHERE id = (
                SELECT id FROM event_inbox
                WHERE channel_id = $1 AND status = 'pending'
                ORDER BY id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, payload, bridge_id, attempts
            """,
            channel_id,
        )
        if row is None:
            return None
        return _to_item(row)

    async def pending_channels(self) -> list[str]:
        """처리 대기 중인 이벤트가 있는 채널 목록. 워커 기동 대상."""
        rows = await self._db.fetch(
            "SELECT DISTINCT channel_id FROM event_inbox WHERE status = 'pending'"
        )
        return [str(r["channel_id"]) for r in rows]

    async def mark_done(self, row_id: int) -> None:
        await self._db.execute(
            "UPDATE event_inbox SET status = 'done', processed_at = now() WHERE id = $1",
            row_id,
        )

    async def mark_failed(self, row_id: int, error: str, *, max_attempts: int) -> bool:
        """실패 기록. 재시도 한계를 넘으면 DLQ 로 보낸다.

        Returns:
            True  DLQ 로 이동 (더 이상 재시도하지 않음)
            False 재시도 대기 상태로 되돌림
        """
        status = await self._db.fetchval(
            """
            UPDATE event_inbox
            SET status = CASE WHEN attempts >= $2 THEN 'dead' ELSE 'pending' END,
                last_error = $3
            WHERE id = $1
            RETURNING status
            """,
            row_id,
            max_attempts,
            error[:2000],
        )
        dead = bool(status == "dead")
        if dead:
            log.error("inbox.moved_to_dlq", row_id=row_id, error=error[:200])
        return dead

    async def requeue_stale(self, *, older_than_seconds: int = 300) -> int:
        """프로세스가 죽어 'processing' 으로 남은 행을 회수한다.

        기동 시 반드시 호출해야 한다. 그러지 않으면 중단 시점에 처리 중이던
        이벤트가 영구히 멈춘 채로 남는다 — NFR-2 위반.
        """
        count = await self._db.fetchval(
            """
            WITH stale AS (
                UPDATE event_inbox
                SET status = 'pending'
                WHERE status = 'processing'
                  AND received_at < now() - make_interval(secs => $1::double precision)
                RETURNING 1
            )
            SELECT count(*) FROM stale
            """,
            float(older_than_seconds),
        )
        n = int(count or 0)
        if n:
            log.warning("inbox.requeued_stale", count=n)
        return n

    async def requeue_all_processing(self) -> int:
        """기동 시 'processing' 전부를 회수한다.

        단일 프로세스 배포에서는 기동 시점에 처리 중인 행이 있을 수 없으므로
        (있다면 직전 크래시의 잔재) 시간 조건 없이 전부 되돌리는 것이 맞다.
        """
        count = await self._db.fetchval(
            """
            WITH stale AS (
                UPDATE event_inbox SET status = 'pending'
                WHERE status = 'processing'
                RETURNING 1
            )
            SELECT count(*) FROM stale
            """
        )
        n = int(count or 0)
        if n:
            log.warning("inbox.requeued_on_startup", count=n)
        return n

    async def stats(self) -> dict[str, int]:
        rows = await self._db.fetch("SELECT status, count(*) AS n FROM event_inbox GROUP BY status")
        return {str(r["status"]): int(r["n"]) for r in rows}


def _to_item(row: Any) -> InboxItem:
    payload = row["payload"]
    data: dict[str, Any] = json.loads(payload) if isinstance(payload, str) else payload
    return InboxItem(
        row_id=int(row["id"]),
        event=Event.from_json(data),
        bridge_id=row["bridge_id"],
        attempts=int(row["attempts"]),
    )
