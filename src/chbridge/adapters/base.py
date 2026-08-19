"""어댑터 계약. (실행계획서 3.3)

Router 는 이 인터페이스만 알고 구현체를 알지 못한다. 그 결과 MM↔MM 은
양쪽이 모두 Mattermost 구현체인 경우일 뿐이며 **별도 코드가 발생하지 않는다.**
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum

from chbridge.cir import Event, FileAttachment, OutboundMessage, Platform


class FileMode(StrEnum):
    """첨부 처리 순서. 플랫폼이 강제한다.

    Mattermost 는 `POST /posts` 에 file_ids 를 실어야 하므로 **게시 전에**
    올려야 하고, Slack 은 chat.postMessage 에 파일 id 를 붙일 수 없어
    **게시 후에** files.completeUploadExternal 로 매달아야 한다.
    정반대라서 한쪽 순서로 통일할 수 없다.
    """

    PRE_UPLOAD = "pre_upload"
    POST_ATTACH = "post_attach"


# 어댑터가 수신 이벤트를 밀어넣는 곳. 파이프라인 진입점.
EventSink = Callable[[Event], Awaitable[None]]


class AdapterError(Exception):
    """어댑터 수준 오류. 재시도 가능 여부를 구분한다."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Adapter(abc.ABC):
    """플랫폼 하나(워크스페이스 하나)를 담당한다."""

    platform: Platform
    workspace_id: str

    @abc.abstractmethod
    async def open(self) -> None:
        """HTTP 세션 준비, 자기 식별자 조회."""

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def listen(self, sink: EventSink) -> None:
        """이벤트 수신 루프.

        연결이 끊기면 **예외를 던지고 반환한다.** 재연결은 Supervisor 가
        백오프와 함께 담당하며, 재시작 시 백필이 실행된다. (PRD 5.9)
        """

    @abc.abstractmethod
    def self_id(self) -> str:
        """자기 발신 판별에 쓰는 식별자. (LoopGuard 방어 ①)

        Mattermost 는 봇의 user_id, Slack 은 bot_id 다.
        플랫폼별로 기준이 다르다는 것이 Phase 0 실측에서 확인됐다.
        """

    @abc.abstractmethod
    async def resolve_channel(self, *, team: str | None, name: str) -> str:
        """사람이 읽는 채널 이름을 채널 id 로 해석한다."""

    @abc.abstractmethod
    async def send(self, channel_id: str, message: OutboundMessage) -> str:
        """메시지를 게시하고 새 메시지 id 를 반환한다."""

    @abc.abstractmethod
    async def edit(self, channel_id: str, message_id: str, message: OutboundMessage) -> None: ...

    @abc.abstractmethod
    async def delete(self, channel_id: str, message_id: str) -> None: ...

    # ------------------------------------------------------------ 첨부 파일

    def max_file_size(self) -> int:
        """이 워크스페이스가 받아주는 첨부 최대 크기(바이트).

        0 이면 미상이라는 뜻이고, 그때는 사전 검사 없이 업로드를 시도한다.
        플랫폼이 알려주는 값을 open() 시점에 받아두는 것이 원칙이다.
        """
        return 0

    def supports_upload(self) -> bool:
        """이 어댑터가 파일 업로드를 구현했는가.

        미구현 어댑터로 가는 첨부는 조용히 사라지지 않고 안내 문구가 된다.
        """
        return False

    def file_mode(self) -> FileMode:
        """첨부를 게시 **전에** 올리는가, **후에** 붙이는가.

        플랫폼이 강제하는 순서라 어댑터가 선언한다. Relay 는 이 값만 보고
        호출 순서를 정하며, 플랫폼 이름을 알 필요가 없다.
        """
        return FileMode.PRE_UPLOAD

    @abc.abstractmethod
    def open_file(self, file: FileAttachment) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        """원본에서 파일 바이트를 스트리밍한다.

        **통째로 메모리에 올리면 안 된다.** 첨부는 수백 MB 가 될 수 있고,
        브릿지는 채널 수만큼 이 작업을 동시에 수행할 수 있다.
        """

    @abc.abstractmethod
    async def upload_file(
        self,
        channel_id: str,
        file: FileAttachment,
        chunks: AsyncIterator[bytes],
        *,
        message_id: str | None = None,
    ) -> str:
        """대상 채널에 업로드하고 **대상 플랫폼의** file id 를 반환한다.

        message_id 는 POST_ATTACH 어댑터에만 전달된다. 방금 게시한 메시지에
        첨부를 매다는 데 쓴다 (Slack 의 thread_ts). PRE_UPLOAD 어댑터는
        게시 전에 호출되므로 이 값을 받지 못하고, 받아도 쓰지 않는다.
        """

    @abc.abstractmethod
    async def backfill(self, channel_id: str, since: str | None) -> list[Event]:
        """단절 구간을 긁어온다. (FR-8.2)

        커서 이후의 이벤트를 오래된 것부터 반환한다. 중복은 event_inbox 의
        UNIQUE 제약이 걸러내므로 여기서 정확할 필요는 없다 — 넉넉하게 가져오는
        편이 유실보다 안전하다.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.platform.value}:{self.workspace_id}>"
