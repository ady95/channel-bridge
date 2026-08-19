"""Mattermost 어댑터.

REST 와 WebSocket 을 모두 자체 구현한다. Phase 0 실측에서 필요한 엔드포인트가
7개뿐임이 확인됐고, 자체 구현하면
  - aiohttp 단일 스택이 유지된다 (mattermostautodriver 는 httpx 도 요구)
  - 타임아웃·재시도·오류 분류를 직접 통제할 수 있다 (NFR-2)
  - WebSocket 수명주기를 Supervisor 와 맞물릴 수 있다

Phase 0 실측 근거 (실행계획서 12장)
  - props.override_username 은 props.from_webhook="true" 와 함께 보내야
    webhook 게시물과 동일하게 렌더링된다
  - 봇이 자기 게시물의 posted 이벤트를 받는다 -> 자기 발신 필터 필수
  - 자기 발신 판별 기준은 post.user_id (Slack 은 bot_id 로 다름)
  - posted 이벤트의 data.post 는 JSON **문자열**이라 한 번 더 파싱해야 한다
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import structlog

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

log = structlog.get_logger(__name__)

# 전달 대상이 아닌 시스템 게시물 (PRD 2.5). type 이 비어 있지 않으면 시스템 메시지다.
_SYSTEM_PREFIX = "system_"

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

# 파일 전송에는 total 을 걸 수 없다. 100MB 를 30초 안에 끝내라는 제약이 되어
# 큰 첨부가 무조건 중단된다. 대신 sock_read 로 "멈춘 연결"만 끊는다.
_FILE_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=120)

# 스트리밍 청크. 메모리 사용량은 (동시 전송 수 x 이 값) 으로 묶인다.
_CHUNK = 256 * 1024


def _pick_name(user: dict[str, Any], fallback: str = "") -> str:
    """nickname > 이름+성 > username 순으로 표시 이름을 고른다."""
    nickname = str(user.get("nickname") or "").strip()
    full = " ".join(
        p for p in (str(user.get("first_name") or ""), str(user.get("last_name") or "")) if p
    ).strip()
    return nickname or full or str(user.get("username") or "") or fallback


class MattermostAdapter(Adapter):
    platform = Platform.MATTERMOST

    def __init__(self, *, workspace_id: str, base_url: str, token: str) -> None:
        self.workspace_id = workspace_id
        self._base = base_url.rstrip("/")
        self._api = f"{self._base}/api/v4"
        self._token = token
        self._session: aiohttp.ClientSession | None = None
        self._self_user_id: str = ""
        # user_id -> 표시 이름. (PRD 5.3)
        self._user_names = NameCache()
        # 서버가 알려주는 첨부 크기 상한. open() 에서 채운다. 0 이면 미상.
        self._max_file_size = 0

    # ------------------------------------------------------------ 수명주기

    async def open(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._token}"}, timeout=_HTTP_TIMEOUT
        )
        me = await self._get("/users/me")
        self._self_user_id = str(me["id"])
        # 크기 상한을 미리 받아둔다. 봇 권한으로도 client config 는 읽을 수 있다.
        # 실패해도 기동을 막지 않는다 — 미상이면 업로드를 시도하고 결과로 판정한다.
        try:
            cfg = await self._get("/config/client", params={"format": "old"})
            self._max_file_size = int(cfg.get("MaxFileSize") or 0)
        except (AdapterError, TypeError, ValueError) as exc:
            log.warning(
                "mm.max_file_size_unknown", workspace=self.workspace_id, error=str(exc)[:120]
            )
        log.info(
            "mm.opened",
            workspace=self.workspace_id,
            bot_username=me.get("username"),
            bot_user_id=self._self_user_id,
            max_file_size=self._max_file_size,
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def self_id(self) -> str:
        return self._self_user_id

    # ------------------------------------------------------------ HTTP

    @property
    def _s(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise AdapterError("어댑터가 열려 있지 않습니다", retryable=False)
        return self._session

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._api}{path}"
        try:
            async with self._s.request(method, url, **kwargs) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    # 4xx 는 대체로 요청 자체가 잘못된 것이므로 재시도해도 같다.
                    # 단 429(레이트 리밋)와 408 은 재시도 가치가 있다.
                    retryable = resp.status >= 500 or resp.status in (408, 429)
                    raise AdapterError(
                        f"{method} {path} -> HTTP {resp.status}: {text[:300]}",
                        retryable=retryable,
                    )
                return json.loads(text) if text else None
        except aiohttp.ClientError as exc:
            raise AdapterError(f"{method} {path} 통신 실패: {exc}", retryable=True) from exc

    async def _get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return await self._request("POST", path, json=payload)

    # ------------------------------------------------------------ 채널 해석

    async def resolve_channel(self, *, team: str | None, name: str) -> str:
        if team is None:
            raise AdapterError(
                f"Mattermost 채널 {name!r} 해석에는 team 이 필요합니다", retryable=False
            )
        data = await self._get(f"/teams/name/{team}/channels/name/{name}")
        return str(data["id"])

    # ------------------------------------------------------------ 게시/편집/삭제

    def _props_for(self, message: OutboundMessage) -> dict[str, Any]:
        """표시 이름 오버라이드 props. (Phase 0 T-0.2 실측 결과)

        from_webhook="true" 가 없으면 override_username 이 렌더링에 반영되지
        않는다. 반드시 함께 보낸다.
        """
        props: dict[str, Any] = {"from_webhook": "true"}
        name = message.display_name()
        if name:
            props["override_username"] = name
        if message.author and message.author.avatar_url:
            props["override_icon_url"] = message.author.avatar_url
        return props

    async def send(self, channel_id: str, message: OutboundMessage) -> str:
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "message": message.text,
            "props": self._props_for(message),
        }
        if message.parent_message_id:
            payload["root_id"] = message.parent_message_id
        if message.file_ids:
            payload["file_ids"] = list(message.file_ids)
        created = await self._post("/posts", payload)
        return str(created["id"])

    # ------------------------------------------------------------ 첨부 파일

    def max_file_size(self) -> int:
        return self._max_file_size

    def supports_upload(self) -> bool:
        return True

    @contextlib.asynccontextmanager
    async def open_file(self, file: FileAttachment) -> AsyncIterator[AsyncIterator[bytes]]:
        url = f"{self._api}/files/{file.source_file_id}"
        try:
            async with self._s.get(url, timeout=_FILE_TIMEOUT) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    raise AdapterError(
                        f"파일 다운로드 실패 {file.source_file_id}: HTTP {resp.status}: {body}",
                        retryable=resp.status >= 500 or resp.status in (408, 429),
                    )
                yield resp.content.iter_chunked(_CHUNK)
        except aiohttp.ClientError as exc:
            raise AdapterError(
                f"파일 다운로드 통신 실패 {file.name}: {exc}", retryable=True
            ) from exc

    async def upload_file(
        self,
        channel_id: str,
        file: FileAttachment,
        chunks: AsyncIterator[bytes],
        *,
        message_id: str | None = None,
    ) -> str:
        # message_id 는 쓰지 않는다. MM 은 PRE_UPLOAD 라 게시 전에 불린다.
        # multipart 대신 단순 바이너리 업로드를 쓴다. aiohttp 의 FormData 는
        # async iterator 를 받아주지만 길이를 모르면 청크 전송이 되는데, MM 의
        # multipart 파서가 그 경로에서 까다롭다. 이 엔드포인트는 원시 바디를
        # 받아주므로 스트리밍이 그대로 통한다.
        params = {"channel_id": channel_id, "filename": file.name}
        headers = {"Content-Type": file.mime_type or "application/octet-stream"}
        try:
            async with self._s.post(
                f"{self._api}/files",
                params=params,
                headers=headers,
                data=chunks,
                timeout=_FILE_TIMEOUT,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    # 413 은 크기 초과. 재시도해도 같으므로 즉시 포기한다.
                    retryable = resp.status >= 500 or resp.status in (408, 429)
                    raise AdapterError(
                        f"파일 업로드 실패 {file.name}: HTTP {resp.status}: {text[:200]}",
                        retryable=retryable,
                    )
                infos = (json.loads(text) or {}).get("file_infos") or []
        except aiohttp.ClientError as exc:
            raise AdapterError(f"파일 업로드 통신 실패 {file.name}: {exc}", retryable=True) from exc

        if not infos:
            raise AdapterError(
                f"파일 업로드 응답에 file_info 가 없습니다: {file.name}", retryable=False
            )
        return str(infos[0]["id"])

    async def edit(self, channel_id: str, message_id: str, message: OutboundMessage) -> None:
        # props 를 다시 보내지 않으면 오버라이드가 유실된다.
        await self._request(
            "PUT",
            f"/posts/{message_id}/patch",
            json={"message": message.text, "props": self._props_for(message)},
        )

    async def delete(self, channel_id: str, message_id: str) -> None:
        try:
            await self._request("DELETE", f"/posts/{message_id}")
        except AdapterError as exc:
            # 이미 지워진 경우는 성공으로 취급한다 (멱등).
            if "HTTP 404" in str(exc):
                log.debug("mm.delete_already_gone", message_id=message_id)
                return
            raise

    # ------------------------------------------------------------ 수신

    async def listen(self, sink: EventSink) -> None:
        ws_url = self._base.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/api/v4/websocket"
        log.info("mm.ws_connecting", workspace=self.workspace_id, url=ws_url)

        async with self._s.ws_connect(ws_url, heartbeat=30) as ws:
            await ws.send_json(
                {"seq": 1, "action": "authentication_challenge", "data": {"token": self._token}}
            )
            async for raw in ws:
                if raw.type is aiohttp.WSMsgType.ERROR:
                    raise AdapterError(f"WebSocket 오류: {ws.exception()}", retryable=True)
                if raw.type is not aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(raw.data)
                if payload.get("event") == "hello":
                    log.info("mm.ws_ready", workspace=self.workspace_id)
                    continue
                event = await self._to_cir(payload)
                if event is not None:
                    await sink(event)

        # 루프를 정상 종료하면 서버가 연결을 닫은 것이다. Supervisor 가 재시작한다.
        raise AdapterError("WebSocket 연결이 종료되었습니다", retryable=True)

    async def _to_cir(self, payload: dict[str, Any]) -> Event | None:
        kind_name = payload.get("event")
        data = payload.get("data") or {}

        if kind_name == "posted":
            post = _parse_post(data.get("post"))
            if post is None or _is_system(post):
                return None
            return await self._message_event(EventKind.MESSAGE_CREATED, post, data)

        if kind_name == "post_edited":
            post = _parse_post(data.get("post"))
            if post is None or _is_system(post):
                return None
            return await self._message_event(EventKind.MESSAGE_EDITED, post, data)

        if kind_name == "post_deleted":
            post = _parse_post(data.get("post"))
            if post is None:
                return None
            return await self._message_event(EventKind.MESSAGE_DELETED, post, data)

        # 프로필이 바뀌면 캐시를 즉시 갱신한다. 전달할 이벤트는 아니지만,
        # 놓치면 이름을 바꿔도 옛 이름으로 계속 전달된다.
        if kind_name == "user_updated":
            user = data.get("user")
            if isinstance(user, dict) and user.get("id"):
                user_id = str(user["id"])
                self._user_names.put(user_id, _pick_name(user))
                log.debug("mm.user_name_refreshed", workspace=self.workspace_id, user_id=user_id)
            return None

        if kind_name in ("reaction_added", "reaction_removed"):
            reaction = _parse_post(data.get("reaction"))
            if reaction is None:
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
                    # 리액션 이벤트에는 channel_id 가 없다. broadcast 에서 얻는다.
                    channel_id=str((payload.get("broadcast") or {}).get("channel_id", "")),
                    message_id=str(reaction["post_id"]),
                ),
                event_key=f"{kind.value}:{reaction['post_id']}:"
                f"{reaction.get('user_id')}:{reaction.get('emoji_name')}",
                emoji=str(reaction.get("emoji_name") or ""),
                reactor_id=str(reaction.get("user_id") or ""),
                created_at_ms=int(reaction.get("create_at") or 0),
                raw=reaction,
            )

        return None

    async def _display_name(self, user_id: str, fallback: str) -> str:
        """사용자의 표시 이름을 조회한다. (PRD 5.3)

        WebSocket 의 data.sender_name 은 "@alice" 처럼 username 이라 사용자가
        기대하는 이름이 아니다. 프로필을 조회해 nickname > 이름+성 > username
        순으로 고른다.

        결과는 캐시하되 TTL 을 둔다. user_updated 이벤트로 즉시 갱신하지만,
        WebSocket 이 끊긴 구간의 변경은 그 이벤트를 놓치기 때문이다.
        """
        cached = self._user_names.get(user_id)
        if cached is not None:
            return cached
        try:
            user = await self._get(f"/users/{user_id}")
        except AdapterError as exc:
            log.debug("mm.user_lookup_failed", user_id=user_id, error=str(exc)[:120])
            return fallback

        name = _pick_name(user, fallback)
        self._user_names.put(user_id, name)
        return name

    async def _message_event(
        self, kind: EventKind, post: dict[str, Any], data: dict[str, Any]
    ) -> Event:
        props = post.get("props") or {}
        user_id = str(post.get("user_id") or "")
        fallback = (
            str(data.get("sender_name") or "").lstrip("@")
            or str(props.get("override_username") or "")
            or "unknown"
        )
        # 복제 메시지는 원 작성자 이름이 props 에 이미 들어 있다. 봇 프로필을
        # 조회하면 "bridgebot" 이 되어버리므로 props 를 우선한다.
        if props.get("override_username"):
            display_name = str(props["override_username"])
        else:
            display_name = await self._display_name(user_id, fallback)

        author = Author(
            platform_user_id=user_id,
            display_name=display_name,
            is_bot=bool(props.get("from_bot") == "true"),
        )
        root = str(post.get("root_id") or "")
        files = _parse_files(post)
        # 백필 커서. 편집도 포착해야 하므로 update_at 을 우선한다.
        position = int(post.get("update_at") or post.get("create_at") or 0)
        return Event(
            kind=kind,
            source=MessageRef(
                platform=self.platform,
                workspace_id=self.workspace_id,
                channel_id=str(post["channel_id"]),
                message_id=str(post["id"]),
            ),
            # MM 은 이벤트 id 를 주지 않으므로 (게시물 id + 종류 + 갱신시각)으로
            # 멱등 키를 만든다. 편집은 update_at 이 바뀌므로 매번 새 키가 된다.
            event_key=f"{kind.value}:{post['id']}:{post.get('update_at') or 0}",
            author=author,
            text=str(post.get("message") or ""),
            parent_message_id=root or None,
            files=files,
            created_at_ms=int(post.get("create_at") or 0),
            cursor=str(position),
            raw=post,
        )

    # ------------------------------------------------------------ 백필

    async def backfill(self, channel_id: str, since: str | None) -> list[Event]:
        if since is None:
            # 커서가 없으면 과거를 끌어오지 않는다. 브릿지 생성 시점 이전의
            # 대화 전달은 명시적 범위 외다 (PRD 7).
            return []
        data = await self._get(f"/channels/{channel_id}/posts", params={"since": since})
        order: list[str] = list(data.get("order") or [])
        posts: dict[str, Any] = data.get("posts") or {}
        events: list[Event] = []
        # order 는 최신순이므로 뒤집어 오래된 것부터 처리한다 (순서 보장).
        for post_id in reversed(order):
            post = posts.get(post_id)
            if not isinstance(post, dict) or _is_system(post):
                continue
            if post.get("delete_at"):
                continue
            events.append(await self._message_event(EventKind.MESSAGE_CREATED, post, {}))
        if events:
            log.info("mm.backfilled", channel_id=channel_id, count=len(events), since=since)
        return events


def _parse_post(value: Any) -> dict[str, Any] | None:
    """MM WebSocket 은 중첩 객체를 JSON 문자열로 보낸다. (T-0.3 실측)"""
    if isinstance(value, str):
        try:
            parsed: Any = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return value if isinstance(value, dict) else None


def _is_system(post: dict[str, Any]) -> bool:
    """시스템 게시물 판별. (PRD 2.5 - 입퇴장·채널명 변경 등은 전달하지 않음)"""
    return str(post.get("type") or "").startswith(_SYSTEM_PREFIX)


def _parse_files(post: dict[str, Any]) -> tuple[FileAttachment, ...]:
    """첨부 메타데이터를 뽑는다.

    metadata.files 에 이름·크기·MIME 이 모두 실려 오므로 추가 조회가 필요 없다.
    (실측 확인) metadata 가 없는 경우에만 file_ids 로 뼈대를 만든다 — 이름을
    모르면 안내 문구도 못 쓰므로 id 를 대신 쓴다.
    """
    metadata = post.get("metadata") or {}
    infos = metadata.get("files")
    if isinstance(infos, list) and infos:
        return tuple(
            FileAttachment(
                source_file_id=str(info.get("id") or ""),
                name=str(info.get("name") or info.get("id") or "file"),
                size=int(info.get("size") or 0),
                mime_type=str(info.get("mime_type")) if info.get("mime_type") else None,
            )
            for info in infos
            if isinstance(info, dict) and info.get("id")
        )

    file_ids = post.get("file_ids")
    if isinstance(file_ids, list):
        return tuple(
            FileAttachment(source_file_id=str(fid), name=str(fid)) for fid in file_ids if fid
        )
    return ()
