"""Canonical Intermediate Representation (CIR).

플랫폼 중립 이벤트 모델. 모든 어댑터는 수신 이벤트를 CIR로 정규화하고,
송신 시 CIR을 대상 플랫폼 형식으로 렌더링한다.

이 구조가 MM↔MM 을 별도 코드 없이 성립시킨다. Router 는 대상 Endpoint 의
플랫폼 종류를 알 필요가 없다. (실행계획서 3.3)

pydantic 을 쓰는 이유: event_inbox 의 JSONB 왕복(직렬화/역직렬화)이
검증과 함께 공짜로 얻어진다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field


class Platform(StrEnum):
    MATTERMOST = "mattermost"
    SLACK = "slack"


class EventKind(StrEnum):
    MESSAGE_CREATED = "message_created"
    MESSAGE_EDITED = "message_edited"
    MESSAGE_DELETED = "message_deleted"
    REACTION_ADDED = "reaction_added"
    REACTION_REMOVED = "reaction_removed"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MessageRef(_Frozen):
    """플랫폼을 가로질러 메시지 하나를 유일하게 지목한다.

    workspace_id 가 반드시 포함되어야 한다. MM↔MM 에서는 platform 이 양쪽
    모두 mattermost 이므로 workspace_id 없이는 서로 다른 서버의 메시지를
    구분할 수 없다.
    """

    platform: Platform
    workspace_id: str
    channel_id: str
    message_id: str

    def key(self) -> tuple[str, str, str, str]:
        return (self.platform.value, self.workspace_id, self.channel_id, self.message_id)

    def __str__(self) -> str:
        return f"{self.platform.value}:{self.workspace_id}/{self.channel_id}/{self.message_id}"


class Author(_Frozen):
    """원 작성자. 복제 메시지의 표시 이름·아이콘 오버라이드에 쓴다. (PRD 5.3)"""

    platform_user_id: str
    display_name: str
    avatar_url: str | None = None
    is_bot: bool = False


class FileAttachment(_Frozen):
    """첨부 파일 메타데이터. 실제 바이트는 전달 시점에 스트리밍한다.

    바이트를 여기 담지 않는 이유: Event 는 event_inbox 에 JSONB 로 저장된다.
    수백 MB 를 DB 에 넣을 수는 없으므로 **참조만 보관하고 전달 순간에
    원본에서 흘려보낸다.** 그 사이 원본이 지워지면 전달은 실패한다.
    """

    source_file_id: str
    name: str
    size: int = 0
    mime_type: str | None = None
    # Slack 은 file id 만으로 내려받을 수 없고 url_private 가 필요하다.
    # Mattermost 는 id 로 충분해 비어 있다.
    source_url: str | None = None


class Event(_Frozen):
    """정규화된 이벤트 하나.

    source 는 "이 이벤트가 가리키는 메시지"다. 리액션 이벤트에서도 대상
    메시지를 가리키므로 항상 존재한다.
    """

    kind: EventKind
    source: MessageRef

    # 이벤트 멱등 키. 같은 이벤트가 두 번 도착하면 두 번째는 폐기된다.
    # Slack: event_id / Mattermost: message_id + kind (MM 은 이벤트 id 를 주지 않음)
    event_key: str

    author: Author | None = None
    text: str = ""

    # 쓰레드 부모의 message_id (원본 플랫폼 기준). 최상위 메시지는 None.
    parent_message_id: str | None = None

    files: tuple[FileAttachment, ...] = ()

    # 리액션 이벤트에서만 사용. 플랫폼 중립 이모지 이름(콜론 없음).
    emoji: str | None = None
    # 리액션을 누른/뗀 사용자. 개수 접힘 추적에 쓴다. (PRD 5.5)
    reactor_id: str | None = None

    created_at_ms: int = 0

    # 백필 커서로 쓰는 플랫폼 고유 위치 표식. (PRD 5.9 / FR-8)
    # Mattermost: update_at 또는 create_at (epoch ms)
    # Slack     : ts ("1786107692.046269")
    # 플랫폼마다 형식이 달라 문자열로 보관한다.
    cursor: str = ""

    # 원본 페이로드. 디버깅과 향후 필드 추가 시의 안전망.
    raw: dict[str, Any] = Field(default_factory=dict)

    def is_thread_reply(self) -> bool:
        return self.parent_message_id is not None

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        return cls.model_validate(data)


class OutboundMessage(_Frozen):
    """어댑터가 대상 플랫폼에 게시할 내용.

    Event 에서 파생되지만 대상 쪽 부모 id 로 이미 변환된 상태다.
    """

    text: str
    author: Author | None
    # 대상 플랫폼 기준 부모 message_id. 최상위면 None.
    parent_message_id: str | None = None
    # **대상 플랫폼**에 이미 업로드된 파일 id. Relay 가 업로드를 마친 뒤 채운다.
    # 원본 첨부 메타데이터(FileAttachment)가 아니라는 점이 중요하다.
    file_ids: tuple[str, ...] = ()
    # 출처 표기용 접미사 (FR-2.3). 예: "Alice Kim (본사MM)"
    source_alias: str | None = None

    def display_name(self) -> str | None:
        """복제 메시지에 표시할 이름. 출처를 괄호로 덧붙인다."""
        if self.author is None:
            return None
        if self.source_alias:
            return f"{self.author.display_name} ({self.source_alias})"
        return self.author.display_name
