"""Slack 어댑터. (T-2.1 ~ T-2.4, T-2.7)

Socket Mode 로 수신하고 Web API 로 송신한다. 인바운드 포트를 열지 않는다
(PRD 4.3).

Phase 0 실측 근거 (실행계획서 12장) — 이 세 가지가 구현을 결정한다.

  1. Slack 도 자기 발신 이벤트를 받는다. 자기 발신 필터가 필수다.
  2. 자기 발신 판별 기준은 **bot_id** 다. username 오버라이드로 게시하면
     `user` 필드가 비어버려 user 기준으로는 판별할 수 없다.
  3. ★ **bot_id 의 위치가 subtype 마다 다르다.**
        (없음), bot_message  -> 최상위 bot_id
        message_changed      -> message.bot_id
        message_deleted      -> previous_message.bot_id
     최상위만 보면 편집·삭제에서 자기 발신을 놓쳐 **루프가 발생한다.**
     NFR-2 의 무관용 지표에 직결되므로 추출 로직을 한 함수로 모았다.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import aiohttp
import structlog
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from chbridge.adapters.base import Adapter, AdapterError, EventSink
from chbridge.adapters.names import NameCache
from chbridge.cir import (
    Author,
    Event,
    EventKind,
    FileAttachment,
    MessageRef,
    OutboundMessage,
    Platform,
)
from chbridge.transform.markdown import (
    markdown_to_slack,
    slack_mention_ids,
    slack_to_markdown,
)

log = structlog.get_logger(__name__)

# 전달 대상 subtype. 이외의 subtype 은 시스템 메시지이므로 무시한다 (PRD 2.5).
# channel_join / channel_leave / channel_topic 등이 여기 걸린다.
_RELAYABLE_SUBTYPES = {None, "bot_message", "thread_broadcast", "file_share"}

_EDIT_SUBTYPE = "message_changed"
_DELETE_SUBTYPE = "message_deleted"

# chat.postMessage 는 채널당 약 1건/초다. (PRD 5.10)
# 드롭이 아니라 지연으로 흡수한다.
_MIN_POST_INTERVAL = 1.1

# Slack 의 첨부 상한. (PRD 5.8 - MM 100MB 와의 비대칭이 여기서 온다)
_MAX_FILE_SIZE = 1024 * 1024 * 1024

# 파일 전송용 타임아웃/청크. MM 어댑터와 같은 근거다 — total 을 걸면 큰
# 첨부가 무조건 중단되므로 sock_read 로 멈춘 연결만 끊는다.
_FILE_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=120)
_CHUNK = 256 * 1024


def _pick_name(user: dict[str, Any], fallback: str = "") -> str:
    """display_name > real_name > name 순으로 표시 이름을 고른다."""
    profile = user.get("profile") or {}
    return (
        str(profile.get("display_name") or "").strip()
        or str(profile.get("real_name") or "").strip()
        or str(user.get("real_name") or "").strip()
        or str(user.get("name") or "")
        or fallback
    )


class SlackAdapter(Adapter):
    platform = Platform.SLACK

    def __init__(self, *, workspace_id: str, bot_token: str, app_token: str) -> None:
        self.workspace_id = workspace_id
        self._web = AsyncWebClient(token=bot_token)
        # 429 를 Retry-After 에 맞춰 자동 재시도한다. 드롭이 아니라 지연으로
        # 흡수하라는 PRD 5.10 요구를 SDK 수준에서 먼저 처리한다.
        self._web.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=3))
        self._app_token = app_token
        self._socket: SocketModeClient | None = None
        self._bot_id = ""
        self._bot_user_id = ""
        self._user_names = NameCache()
        # 채널별 마지막 게시 시각. 레이트 리밋 게이트용.
        self._last_post: dict[str, float] = {}
        self._post_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------ 수명주기

    async def open(self) -> None:
        auth = await self._call("auth_test")
        self._bot_id = str(auth.get("bot_id") or "")
        self._bot_user_id = str(auth.get("user_id") or "")
        log.info(
            "slack.opened",
            workspace=self.workspace_id,
            team=auth.get("team"),
            bot_id=self._bot_id,
            bot_user=auth.get("user"),
        )

    async def close(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.disconnect()  # type: ignore[no-untyped-call]
                await self._socket.close()  # type: ignore[no-untyped-call]
            self._socket = None

    def self_id(self) -> str:
        # ★ user_id 가 아니라 bot_id 다. (Phase 0 실측)
        return self._bot_id

    # ------------------------------------------------------------ Web API

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Web API 호출. 오류를 AdapterError 로 정규화한다."""
        try:
            fn = getattr(self._web, method)
            response = await fn(**kwargs)
        except Exception as exc:  # slack_sdk 는 SlackApiError 등 여러 예외를 던진다
            text = str(exc)
            # ratelimited 는 slack_sdk 의 재시도 핸들러가 처리하지만, 소진 시
            # 여기까지 온다. 재시도 가치가 있다.
            retryable = "ratelimited" in text or "timeout" in text.lower()
            # 잘못된 인자·권한 부족은 반복해도 같으므로 즉시 DLQ 로 보낸다.
            for fatal in ("invalid_auth", "missing_scope", "not_in_channel", "channel_not_found"):
                if fatal in text:
                    retryable = False
            raise AdapterError(f"slack {method} 실패: {text[:300]}", retryable=retryable) from exc
        return cast(dict[str, Any], response.data)

    async def _rate_gate(self, channel_id: str) -> None:
        """채널당 게시 간격을 강제한다. (PRD 5.10)"""
        lock = self._post_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            elapsed = time.monotonic() - self._last_post.get(channel_id, 0.0)
            if elapsed < _MIN_POST_INTERVAL:
                await asyncio.sleep(_MIN_POST_INTERVAL - elapsed)
            self._last_post[channel_id] = time.monotonic()

    async def _display_name(self, user_id: str) -> str:
        if not user_id:
            return "unknown"
        cached = self._user_names.get(user_id)
        if cached is not None:
            return cached
        try:
            data = await self._call("users_info", user=user_id)
        except AdapterError as exc:
            log.debug("slack.user_lookup_failed", user_id=user_id, error=str(exc)[:120])
            return user_id
        name = _pick_name(data.get("user") or {}, user_id)
        self._user_names.put(user_id, name)
        return name

    # ------------------------------------------------------------ 채널 해석

    async def resolve_channel(self, *, team: str | None, name: str) -> str:
        """채널 이름 -> id. Slack 은 워크스페이스 전역이라 team 이 불필요하다."""
        cursor: str | None = None
        wanted = name.lstrip("#")
        while True:
            data = await self._call(
                "conversations_list",
                limit=200,
                types="public_channel,private_channel",
                exclude_archived=True,
                **({"cursor": cursor} if cursor else {}),
            )
            for channel in data.get("channels") or []:
                if channel.get("name") == wanted:
                    return str(channel["id"])
            cursor = ((data.get("response_metadata") or {}).get("next_cursor")) or ""
            if not cursor:
                raise AdapterError(
                    f"Slack 채널 {name!r} 을 찾을 수 없습니다. 봇이 초대되어 있는지 확인하세요.",
                    retryable=False,
                )

    # ------------------------------------------------------------ 게시/편집/삭제

    async def send(self, channel_id: str, message: OutboundMessage) -> str:
        await self._rate_gate(channel_id)
        payload: dict[str, Any] = {
            "channel": channel_id,
            "text": markdown_to_slack(message.text),
        }
        name = message.display_name()
        if name:
            # chat:write.customize 스코프가 있어야 적용된다. (Phase 0 T-0.5)
            payload["username"] = name
        if message.author and message.author.avatar_url:
            payload["icon_url"] = message.author.avatar_url
        if message.parent_message_id:
            payload["thread_ts"] = message.parent_message_id

        data = await self._call("chat_postMessage", **payload)
        return str(data["ts"])

    async def edit(self, channel_id: str, message_id: str, message: OutboundMessage) -> None:
        # chat.update 는 username 을 다시 받지 않는다. 최초 게시 시의
        # 표시 이름이 유지되므로 텍스트만 갱신하면 된다.
        await self._call(
            "chat_update",
            channel=channel_id,
            ts=message_id,
            text=markdown_to_slack(message.text),
        )

    async def delete(self, channel_id: str, message_id: str) -> None:
        try:
            await self._call("chat_delete", channel=channel_id, ts=message_id)
        except AdapterError as exc:
            # 이미 지워졌으면 성공으로 취급한다 (멱등).
            if "message_not_found" in str(exc):
                log.debug("slack.delete_already_gone", ts=message_id)
                return
            raise

    # ------------------------------------------------------------ 수신

    async def listen(self, sink: EventSink) -> None:
        """Socket Mode 수신 루프.

        ★ 주의: `slack_sdk` 의 SocketModeClient 는 **내부적으로 자동 재연결한다.**
        따라서 연결이 끊겨도 이 코루틴은 반환하지 않고, Supervisor 도
        재시작하지 않는다. 즉 **재연결 시점에 백필이 돌지 않는다.**
        그 공백은 app.py 의 주기적 정합성 스윕(`_reconciler`)이 메운다.
        Mattermost 어댑터와 복구 경로가 다르다는 점을 기억해야 한다.
        """
        socket = SocketModeClient(app_token=self._app_token, web_client=self._web)

        async def handle(client: SocketModeClient, request: SocketModeRequest) -> None:
            # Slack 은 3초 내 ack 를 요구한다. 처리보다 먼저 응답한다.
            await client.send_socket_mode_response(
                SocketModeResponse(envelope_id=request.envelope_id)
            )
            if request.type != "events_api":
                return
            try:
                event = await self._to_cir(request.payload or {})
            except Exception as exc:  # 한 이벤트 실패가 연결을 끊어선 안 된다
                log.error("slack.parse_failed", error=str(exc)[:300], exc_info=True)
                return
            if event is not None:
                await sink(event)

        socket.socket_mode_request_listeners.append(handle)  # type: ignore[arg-type]
        self._socket = socket
        try:
            await socket.connect()  # type: ignore[no-untyped-call]
            log.info("slack.socket_ready", workspace=self.workspace_id)
            # 취소될 때까지 머문다. 폴링하지 않는다.
            await asyncio.Event().wait()
        finally:
            with contextlib.suppress(Exception):
                await socket.disconnect()  # type: ignore[no-untyped-call]
            self._socket = None

    # ------------------------------------------------------------ 이벤트 -> CIR

    async def _to_cir(self, envelope: dict[str, Any]) -> Event | None:
        event = envelope.get("event") or {}
        kind_name = event.get("type")
        # Slack 이 주는 이벤트 고유 id. 멱등 키의 정본이다.
        event_id = str(envelope.get("event_id") or "")

        if kind_name == "message":
            return await self._message_event(event, event_id)

        # MM 의 user_updated 와 같은 역할. 전달 대상은 아니고 캐시만 갱신한다.
        if kind_name == "user_change":
            user = event.get("user")
            if isinstance(user, dict) and user.get("id"):
                user_id = str(user["id"])
                self._user_names.put(user_id, _pick_name(user, user_id))
                log.debug("slack.user_name_refreshed", workspace=self.workspace_id, user_id=user_id)
            return None

        if kind_name in ("reaction_added", "reaction_removed"):
            item = event.get("item") or {}
            if item.get("type") != "message":
                return None
            kind = (
                EventKind.REACTION_ADDED
                if kind_name == "reaction_added"
                else EventKind.REACTION_REMOVED
            )
            return Event(
                kind=kind,
                source=MessageRef(
                    platform=self.platform,
                    workspace_id=self.workspace_id,
                    channel_id=str(item.get("channel") or ""),
                    message_id=str(item.get("ts") or ""),
                ),
                event_key=event_id or f"{kind.value}:{item.get('ts')}:{event.get('user')}",
                emoji=str(event.get("reaction") or ""),
                reactor_id=str(event.get("user") or ""),
                created_at_ms=_ts_to_ms(str(event.get("event_ts") or "")),
                cursor=str(event.get("event_ts") or ""),
                raw=event,
            )

        return None

    async def _message_event(self, event: dict[str, Any], event_id: str) -> Event | None:
        subtype = event.get("subtype")
        channel_id = str(event.get("channel") or "")

        # --- 편집 ---
        if subtype == _EDIT_SUBTYPE:
            inner = event.get("message") or {}
            if inner.get("subtype") not in _RELAYABLE_SUBTYPES:
                return None
            return Event(
                kind=EventKind.MESSAGE_EDITED,
                source=MessageRef(
                    platform=self.platform,
                    workspace_id=self.workspace_id,
                    channel_id=channel_id,
                    message_id=str(inner.get("ts") or ""),
                ),
                event_key=event_id
                or f"edited:{inner.get('ts')}:{inner.get('edited', {}).get('ts')}",
                author=await self._author_of(inner),
                text=await self._to_markdown(str(inner.get("text") or "")),
                parent_message_id=_parent_of(inner),
                created_at_ms=_ts_to_ms(str(inner.get("ts") or "")),
                cursor=str(event.get("event_ts") or ""),
                raw=event,
            )

        # --- 삭제 ---
        if subtype == _DELETE_SUBTYPE:
            previous = event.get("previous_message") or {}
            return Event(
                kind=EventKind.MESSAGE_DELETED,
                source=MessageRef(
                    platform=self.platform,
                    workspace_id=self.workspace_id,
                    channel_id=channel_id,
                    message_id=str(event.get("deleted_ts") or ""),
                ),
                event_key=event_id or f"deleted:{event.get('deleted_ts')}",
                # ★ 삭제 이벤트의 식별 정보는 previous_message 에 있다.
                #   이걸 놓치면 자기 발신 삭제를 걸러내지 못해 루프가 된다.
                author=await self._author_of(previous),
                created_at_ms=_ts_to_ms(str(event.get("deleted_ts") or "")),
                cursor=str(event.get("event_ts") or ""),
                raw=event,
            )

        # --- 신규 ---
        if subtype not in _RELAYABLE_SUBTYPES:
            # channel_join 등 시스템 메시지 (PRD 2.5)
            return None

        return Event(
            kind=EventKind.MESSAGE_CREATED,
            source=MessageRef(
                platform=self.platform,
                workspace_id=self.workspace_id,
                channel_id=channel_id,
                message_id=str(event.get("ts") or ""),
            ),
            event_key=event_id or f"created:{event.get('ts')}",
            author=await self._author_of(event),
            text=await self._to_markdown(str(event.get("text") or "")),
            parent_message_id=_parent_of(event),
            files=_parse_files(event),
            created_at_ms=_ts_to_ms(str(event.get("ts") or "")),
            cursor=str(event.get("event_ts") or event.get("ts") or ""),
            raw=event,
        )

    async def _author_of(self, message: dict[str, Any]) -> Author:
        """작성자 정보 추출.

        ★ platform_user_id 에는 **self_id() 와 비교 가능한 값**을 넣는다.
          LoopGuard 가 플랫폼을 모르고도 자기 발신을 판별할 수 있게 하는 규약이다.
          봇 메시지는 bot_id, 사람 메시지는 user id 다.
        """
        bot_id = str(message.get("bot_id") or "")
        if bot_id:
            # 봇 메시지. username 오버라이드가 있으면 그것이 원 작성자 이름이다.
            return Author(
                platform_user_id=bot_id,
                display_name=str(message.get("username") or "bot"),
                is_bot=True,
            )
        user_id = str(message.get("user") or "")
        return Author(
            platform_user_id=user_id,
            display_name=await self._display_name(user_id),
            is_bot=False,
        )

    async def _to_markdown(self, text: str) -> str:
        """mrkdwn -> Markdown. 멘션 이름을 미리 조회해 넘긴다."""
        ids = slack_mention_ids(text)
        users = {uid: await self._display_name(uid) for uid in ids} if ids else {}
        return slack_to_markdown(text, users=users)

    # ------------------------------------------------------------ 첨부 파일

    def max_file_size(self) -> int:
        return _MAX_FILE_SIZE

    def supports_upload(self) -> bool:
        # ★ 미구현. Slack 은 chat.postMessage 에 파일 id 를 붙일 수 없고,
        #   files_upload_v2 가 파일을 **별도 메시지로** 게시한다. 그래서 본문과
        #   첨부가 한 메시지로 합쳐지지 않고, username 오버라이드(FR-2.3)도
        #   파일 메시지에는 적용되지 않는다. 어느 쪽을 포기할지 정하지 않은 채
        #   구현하면 되돌리기 어려우므로 보류한다.
        #   그동안 MM->Slack 첨부는 조용히 사라지지 않고 안내 문구가 된다.
        return False

    @contextlib.asynccontextmanager
    async def open_file(self, file: FileAttachment) -> AsyncIterator[AsyncIterator[bytes]]:
        """url_private 에서 내려받는다. 봇 토큰이 있어야 열린다."""
        if not file.source_url:
            raise AdapterError(
                f"Slack 첨부에 url_private 이 없습니다: {file.name}", retryable=False
            )
        headers = {"Authorization": f"Bearer {self._web.token}"}
        session = aiohttp.ClientSession(timeout=_FILE_TIMEOUT)
        try:
            async with session.get(file.source_url, headers=headers) as resp:
                if resp.status >= 400:
                    raise AdapterError(
                        f"Slack 파일 다운로드 실패 {file.name}: HTTP {resp.status}",
                        retryable=resp.status >= 500 or resp.status in (408, 429),
                    )
                yield resp.content.iter_chunked(_CHUNK)
        except aiohttp.ClientError as exc:
            raise AdapterError(
                f"Slack 파일 다운로드 통신 실패 {file.name}: {exc}", retryable=True
            ) from exc
        finally:
            await session.close()

    async def upload_file(
        self, channel_id: str, file: FileAttachment, chunks: AsyncIterator[bytes]
    ) -> str:
        raise AdapterError(
            "Slack 파일 업로드는 아직 구현되지 않았습니다 (supports_upload() 주석 참고)",
            retryable=False,
        )

    # ------------------------------------------------------------ 백필

    async def backfill(self, channel_id: str, since: str | None) -> list[Event]:
        """단절 구간 복구. (FR-8.2)

        주의: conversations.history 는 **쓰레드 답글을 포함하지 않는다.**
        답글만 오간 구간을 놓치면 조용한 유실이 되므로, 답글이 있는 메시지에
        대해 conversations.replies 를 추가로 조회한다.
        """
        if since is None:
            # 첫 기동. 과거를 끌어오지 않는다 (PRD 7 범위 외).
            return []

        data = await self._call(
            "conversations_history", channel=channel_id, oldest=since, limit=200, inclusive=False
        )
        messages: list[dict[str, Any]] = list(data.get("messages") or [])

        # 쓰레드 답글 보강
        for parent in list(messages):
            reply_count = int(parent.get("reply_count") or 0)
            latest = str(parent.get("latest_reply") or "")
            if reply_count and latest and _ts_to_ms(latest) > _ts_to_ms(since):
                replies = await self._call(
                    "conversations_replies",
                    channel=channel_id,
                    ts=str(parent.get("thread_ts") or parent.get("ts")),
                    oldest=since,
                    limit=200,
                )
                for reply in replies.get("messages") or []:
                    # 부모 자신은 history 에 이미 있다
                    if str(reply.get("ts")) != str(parent.get("ts")):
                        messages.append(reply)

        events: list[Event] = []
        # 오래된 것부터 처리해야 순서와 쓰레드 부모 매핑이 성립한다.
        for message in sorted(messages, key=lambda m: _ts_to_ms(str(m.get("ts") or ""))):
            if message.get("subtype") not in _RELAYABLE_SUBTYPES:
                continue
            message = {**message, "channel": channel_id}
            event = await self._message_event(message, "")
            if event is not None:
                events.append(event)

        if events:
            log.info("slack.backfilled", channel_id=channel_id, count=len(events), since=since)
        return events


def _parse_files(event: dict[str, Any]) -> tuple[FileAttachment, ...]:
    """첨부 메타데이터를 뽑는다.

    Slack 은 file id 만으로 내려받을 수 없다. url_private 을 함께 보관한다.
    """
    files = event.get("files")
    if not isinstance(files, list):
        return ()
    return tuple(
        FileAttachment(
            source_file_id=str(f.get("id") or ""),
            name=str(f.get("name") or f.get("title") or "file"),
            size=int(f.get("size") or 0),
            mime_type=str(f.get("mimetype")) if f.get("mimetype") else None,
            source_url=str(f.get("url_private")) if f.get("url_private") else None,
        )
        for f in files
        if isinstance(f, dict) and f.get("id")
    )


def _parent_of(message: dict[str, Any]) -> str | None:
    """쓰레드 부모 ts. 부모 자신은 thread_ts == ts 이므로 None 으로 본다."""
    thread_ts = str(message.get("thread_ts") or "")
    if not thread_ts or thread_ts == str(message.get("ts") or ""):
        return None
    return thread_ts


def _ts_to_ms(ts: str) -> int:
    """Slack ts("1786107692.046269") -> epoch milliseconds."""
    try:
        return int(float(ts) * 1000)
    except ValueError:
        return 0
