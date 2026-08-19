"""MessageLink 저장소 — 시스템의 심장. (PRD 5.2)

Origin 과 Replica 들을 group_id 로 묶는다. 편집·삭제·리액션·쓰레드 전달이
모두 이 테이블 조회에 의존한다.

두 개의 핫패스가 있다.
  1) 수신 메시지가 Replica 인지 판정   -> LoopGuard 방어 ② (PRD 5.1)
  2) group_id 로 반대편 메시지 찾기     -> 편집/삭제/리액션/쓰레드
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from chbridge.cir import MessageRef, Platform
from chbridge.store.db import Database

log = structlog.get_logger(__name__)


class MessageLink:
    """message_links 한 행."""

    __slots__ = ("bridge_id", "group_id", "is_origin", "ref")

    def __init__(self, *, group_id: uuid.UUID, bridge_id: str, ref: MessageRef, is_origin: bool):
        self.group_id = group_id
        self.bridge_id = bridge_id
        self.ref = ref
        self.is_origin = is_origin

    def __repr__(self) -> str:
        kind = "origin" if self.is_origin else "replica"
        return f"<MessageLink {kind} {self.ref} group={self.group_id}>"


def _to_link(row: Any) -> MessageLink:
    return MessageLink(
        group_id=row["group_id"],
        bridge_id=row["bridge_id"],
        ref=MessageRef(
            platform=Platform(row["platform"]),
            workspace_id=row["workspace_id"],
            channel_id=row["channel_id"],
            message_id=row["message_id"],
        ),
        is_origin=row["is_origin"],
    )


class MessageLinkStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -------------------------------------------------------------- 조회

    async def find(self, ref: MessageRef) -> MessageLink | None:
        """레퍼런스로 링크 한 건 조회. LoopGuard 방어 ② 의 핫패스."""
        row = await self._db.fetchrow(
            """
            SELECT group_id, bridge_id, platform, workspace_id, channel_id,
                   message_id, is_origin
            FROM message_links
            WHERE platform = $1 AND workspace_id = $2
              AND channel_id = $3 AND message_id = $4
            """,
            ref.platform.value,
            ref.workspace_id,
            ref.channel_id,
            ref.message_id,
        )
        return None if row is None else _to_link(row)

    async def is_replica(self, ref: MessageRef) -> bool:
        """이 메시지가 브릿지가 만든 복제본인가.

        True 면 폐기해야 한다. 그러지 않으면 무한 증식한다.
        """
        value = await self._db.fetchval(
            """
            SELECT is_origin FROM message_links
            WHERE platform = $1 AND workspace_id = $2
              AND channel_id = $3 AND message_id = $4
            """,
            ref.platform.value,
            ref.workspace_id,
            ref.channel_id,
            ref.message_id,
        )
        return value is False

    async def group_members(self, group_id: uuid.UUID) -> list[MessageLink]:
        rows = await self._db.fetch(
            """
            SELECT group_id, bridge_id, platform, workspace_id, channel_id,
                   message_id, is_origin
            FROM message_links
            WHERE group_id = $1
            ORDER BY is_origin DESC, id
            """,
            group_id,
        )
        return [_to_link(r) for r in rows]

    async def counterparts(self, ref: MessageRef) -> list[MessageLink]:
        """같은 그룹의 다른 플랫폼 메시지들.

        편집·삭제·리액션을 반대편에 반영할 때 대상을 찾는 데 쓴다.
        """
        link = await self.find(ref)
        if link is None:
            return []
        return [m for m in await self.group_members(link.group_id) if m.ref != ref]

    async def replica_in(self, group_id: uuid.UUID, *, endpoint_ref: MessageRef) -> str | None:
        """특정 채널에 있는 이 그룹의 메시지 id.

        쓰레드 부모 변환에 쓴다. endpoint_ref 는 대상 채널을 지목하기 위한
        것이므로 message_id 는 무시된다.
        """
        value = await self._db.fetchval(
            """
            SELECT message_id FROM message_links
            WHERE group_id = $1 AND platform = $2
              AND workspace_id = $3 AND channel_id = $4
            LIMIT 1
            """,
            group_id,
            endpoint_ref.platform.value,
            endpoint_ref.workspace_id,
            endpoint_ref.channel_id,
        )
        return None if value is None else str(value)

    async def find_group_for(self, ref: MessageRef) -> uuid.UUID | None:
        link = await self.find(ref)
        return None if link is None else link.group_id

    # -------------------------------------------------------------- 기록

    async def record_origin(
        self, *, bridge_id: str, ref: MessageRef, author_ref: str | None
    ) -> uuid.UUID:
        """원본 메시지를 새 그룹으로 등록한다.

        이미 등록되어 있으면 기존 group_id 를 반환한다 (재처리 안전).
        """
        group_id = uuid.uuid4()
        value = await self._db.fetchval(
            """
            INSERT INTO message_links
                (bridge_id, group_id, platform, workspace_id, channel_id,
                 message_id, is_origin, author_ref)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, $7)
            ON CONFLICT (platform, workspace_id, channel_id, message_id)
                DO UPDATE SET group_id = message_links.group_id
            RETURNING group_id
            """,
            bridge_id,
            group_id,
            ref.platform.value,
            ref.workspace_id,
            ref.channel_id,
            ref.message_id,
            author_ref,
        )
        return uuid.UUID(str(value))

    async def record_replica(
        self, *, bridge_id: str, group_id: uuid.UUID, ref: MessageRef, author_ref: str | None
    ) -> None:
        """복제 메시지를 기존 그룹에 붙인다.

        ★ 이 기록이 LoopGuard 방어 ② 를 성립시킨다. 복제 직후 도착하는
          자기 발신 이벤트가 여기서 걸러진다. 따라서 게시 성공 직후
          지체 없이 기록해야 한다.
        """
        await self._db.execute(
            """
            INSERT INTO message_links
                (bridge_id, group_id, platform, workspace_id, channel_id,
                 message_id, is_origin, author_ref)
            VALUES ($1, $2, $3, $4, $5, $6, FALSE, $7)
            ON CONFLICT (platform, workspace_id, channel_id, message_id) DO NOTHING
            """,
            bridge_id,
            group_id,
            ref.platform.value,
            ref.workspace_id,
            ref.channel_id,
            ref.message_id,
            author_ref,
        )

    async def delete_group(self, group_id: uuid.UUID) -> None:
        await self._db.execute("DELETE FROM message_links WHERE group_id = $1", group_id)
