"""루프 방지 3중 방어. (PRD 5.1)

NFR-2 의 "루프 발생 0건"은 무관용 지표다. 루프는 리팩터링 중 재발하기 가장
쉬운 결함이므로 판정을 이 한 곳에 모으고 단위 테스트로 고정한다.

Phase 0 실측에서 확인된 사실 (실행계획서 12장)
  - Mattermost 도 Slack 도 **봇이 자기 발신 이벤트를 받는다.**
    따라서 방어 ①은 선택이 아니라 필수다. 없으면 첫 전달 즉시 무한 증식한다.
  - 자기 발신 판별 기준이 플랫폼마다 다르다.
      Mattermost : post.user_id
      Slack      : bot_id  (오버라이드 게시 시 user 필드가 비어 버린다)
    어댑터가 Event.author.platform_user_id 에 "self_id() 와 비교 가능한 값"을
    채워 넣기로 정한다. 그래서 이 모듈은 플랫폼을 몰라도 된다.
"""

from __future__ import annotations

from enum import StrEnum

import structlog

from chbridge.cir import Event
from chbridge.store.links import MessageLinkStore

log = structlog.get_logger(__name__)


class DropReason(StrEnum):
    SELF_SENT = "self_sent"
    IS_REPLICA = "is_replica"
    BOT_MESSAGE = "bot_message"
    NO_BRIDGE = "no_bridge"
    EMPTY = "empty"


class LoopGuard:
    def __init__(self, links: MessageLinkStore, self_ids: dict[str, str]) -> None:
        """self_ids: workspace_id -> 그 워크스페이스에서의 자기 식별자."""
        self._links = links
        self._self_ids = self_ids

    async def evaluate(self, event: Event, *, relay_bot_messages: bool) -> DropReason | None:
        """폐기해야 하면 이유를, 통과시켜야 하면 None 을 반환한다."""

        # --- 방어 ① 자기 발신 무시 (값싼 1차 필터) ---
        # 매핑 조회보다 먼저 둔다. DB 왕복 없이 대부분의 루프를 차단한다.
        if self._is_self_sent(event):
            return DropReason.SELF_SENT

        # --- 방어 ② 매핑 조회 (최종 방어선) ---
        # 플랫폼 기능에 의존하지 않는다. ①이 어떤 이유로 뚫려도 여기서 막힌다.
        # 예: 복제 메시지를 사람이 직접 편집한 경우 작성자는 사람이지만
        #     대상 메시지는 Replica 이므로 원본으로 역전파해선 안 된다. (FR-6.3)
        if await self._links.is_replica(event.source):
            return DropReason.IS_REPLICA

        # --- 봇 메시지 정책 (FR-2.4) ---
        # 루프 방어가 아니라 옵션이지만, 판정 지점을 한 곳에 모아둔다.
        if event.author is not None and event.author.is_bot and not relay_bot_messages:
            return DropReason.BOT_MESSAGE

        return None

    def _is_self_sent(self, event: Event) -> bool:
        if event.author is None:
            return False
        expected = self._self_ids.get(event.source.workspace_id)
        if not expected:
            return False
        return event.author.platform_user_id == expected

    def register(self, workspace_id: str, self_id: str) -> None:
        self._self_ids[workspace_id] = self_id
