"""첨부 전달 회귀 테스트.

★ 이 파일이 막는 것: 첨부가 **조용히 사라지는 것**. 초기 구현은 텍스트만
  전달하고 파일을 버렸는데, 받는 쪽에서는 첨부가 있었다는 사실조차 알 수
  없었다.

여기서 고정하는 규약은 셋이다.
  1. 전달 못 한 첨부는 반드시 안내 문구가 된다 (이유 불문).
  2. 첨부 실패가 본문 전달을 막지 않는다.
  3. 바이트는 스트림으로 통과한다 (Event 에 담기지 않는다).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest

from chbridge.adapters.base import Adapter, AdapterError, FileMode
from chbridge.adapters.mattermost import _parse_files
from chbridge.cir import FileAttachment, Platform
from chbridge.relay import Relay
from chbridge.router import Endpoint
from chbridge.transform.files import SkipReason, append_notice, human_size, skip_notice

MB = 1024 * 1024


# ------------------------------------------------------------------ 안내 문구


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "크기 미상"), (512, "512B"), (1536, "1.5KB"), (100 * MB, "100.0MB")],
)
def test_사람이_읽는_크기(size: int, expected: str) -> None:
    assert human_size(size) == expected


def test_전달_못_한_첨부는_이유와_함께_문구가_된다() -> None:
    notice = skip_notice(
        [
            (
                FileAttachment(source_file_id="f1", name="report.zip", size=340 * MB),
                SkipReason.TOO_LARGE,
            )
        ],
        limit=100 * MB,
    )
    assert "report.zip" in notice
    assert "340.0MB" in notice
    assert "100.0MB" in notice


def test_전달할_첨부가_모두_성공하면_문구가_없다() -> None:
    assert skip_notice([]) == ""
    assert append_notice("본문", "") == "본문"


def test_본문이_비어도_안내는_남는다() -> None:
    # 파일만 올린 메시지가 통째로 사라지면 안 된다.
    assert append_notice("", "📎 안내") == "📎 안내"


# ------------------------------------------------------- MM 첨부 메타 파싱


def test_metadata_files_에서_이름과_크기를_읽는다() -> None:
    files = _parse_files(
        {
            "file_ids": ["abc"],
            "metadata": {
                "files": [
                    {"id": "abc", "name": "probe.txt", "size": 180, "mime_type": "text/plain"}
                ]
            },
        }
    )
    assert files == (
        FileAttachment(source_file_id="abc", name="probe.txt", size=180, mime_type="text/plain"),
    )


def test_metadata_가_없으면_file_ids_로_뼈대를_만든다() -> None:
    # 이름을 모르면 안내 문구도 못 쓰므로 id 라도 남긴다.
    files = _parse_files({"file_ids": ["abc"]})
    assert len(files) == 1
    assert files[0].source_file_id == "abc"


def test_첨부가_없으면_빈_튜플() -> None:
    assert _parse_files({"message": "그냥 텍스트"}) == ()


# ------------------------------------------------------------ 전달 동작


class FakeAdapter(Adapter):
    """바이트가 실제로 통과하는지 보기 위한 최소 구현."""

    platform = Platform.MATTERMOST

    def __init__(
        self,
        *,
        limit: int = 0,
        uploads_ok: bool = True,
        supports: bool = True,
        mode: FileMode = FileMode.PRE_UPLOAD,
    ) -> None:
        self.workspace_id = "ws"
        self._limit = limit
        self._uploads_ok = uploads_ok
        self._supports = supports
        self._mode = mode
        self.received: dict[str, bytes] = {}
        self.attached_to: dict[str, str | None] = {}
        self.sent: list[object] = []
        self.edited: list[object] = []

    def file_mode(self) -> FileMode:
        return self._mode

    def max_file_size(self) -> int:
        return self._limit

    def supports_upload(self) -> bool:
        return self._supports

    @contextlib.asynccontextmanager
    async def open_file(self, file: FileAttachment) -> AsyncIterator[AsyncIterator[bytes]]:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"hello-"
            yield b"world"

        yield chunks()

    async def upload_file(
        self,
        channel_id: str,
        file: FileAttachment,
        chunks: AsyncIterator[bytes],
        *,
        message_id: str | None = None,
    ) -> str:
        if not self._uploads_ok:
            raise AdapterError("업로드 실패", retryable=False)
        self.received[file.name] = b"".join([c async for c in chunks])
        self.attached_to[file.name] = message_id
        return f"uploaded-{file.name}"

    # 이 테스트에서 쓰지 않는 계약
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def listen(self, sink: object) -> None: ...  # type: ignore[override]
    def self_id(self) -> str:
        return "self"

    async def resolve_channel(self, *, team: str | None, name: str) -> str:
        return "c"

    async def send(self, channel_id: str, message: object) -> str:  # type: ignore[override]
        self.sent.append(message)
        return "m"

    async def edit(self, channel_id: str, message_id: str, message: object) -> None:  # type: ignore[override]
        self.edited.append(message)

    async def delete(self, channel_id: str, message_id: str) -> None: ...
    async def backfill(self, channel_id: str, since: str | None) -> list[object]:  # type: ignore[override]
        return []


def endpoint(ws: str) -> Endpoint:
    return Endpoint(
        id="e",
        bridge_id="b",
        platform=Platform.MATTERMOST,
        workspace_id=ws,
        channel_id="c",
        alias="A",
    )


def relay_with(src: Adapter, dst: Adapter) -> Relay:
    # _transfer_files 는 _adapters 만 사용한다.
    return Relay(router=None, links=None, guard=None, adapters={"src": src, "dst": dst})  # type: ignore[arg-type]


async def transfer(
    relay: Relay, files: tuple[FileAttachment, ...], *, relay_files: bool = True
) -> tuple[tuple[str, ...], str]:
    from chbridge.cir import Event, EventKind, MessageRef

    event = Event(
        kind=EventKind.MESSAGE_CREATED,
        source=MessageRef(
            platform=Platform.MATTERMOST, workspace_id="src", channel_id="c", message_id="m"
        ),
        event_key="k",
        files=files,
    )
    return await relay._transfer_files(
        event, endpoint("src"), endpoint("dst"), relay_files=relay_files
    )


@pytest.mark.asyncio
async def test_바이트가_원본에서_대상으로_그대로_통과한다() -> None:
    dst = FakeAdapter()
    relay = relay_with(FakeAdapter(), dst)
    ids, notice = await transfer(
        relay, (FileAttachment(source_file_id="f1", name="a.txt", size=11),)
    )

    assert ids == ("uploaded-a.txt",)
    assert notice == ""
    assert dst.received["a.txt"] == b"hello-world"


@pytest.mark.asyncio
async def test_크기_초과는_올려보지도_않고_문구가_된다() -> None:
    dst = FakeAdapter(limit=100 * MB)
    relay = relay_with(FakeAdapter(), dst)
    ids, notice = await transfer(
        relay, (FileAttachment(source_file_id="f1", name="big.zip", size=340 * MB),)
    )

    assert ids == ()
    assert dst.received == {}  # 대역폭을 낭비하지 않는다
    assert "big.zip" in notice and "100.0MB" in notice


@pytest.mark.asyncio
async def test_상한이_미상이면_일단_시도한다() -> None:
    # limit=0 은 "모른다"는 뜻이지 "0바이트"가 아니다.
    dst = FakeAdapter(limit=0)
    relay = relay_with(FakeAdapter(), dst)
    ids, notice = await transfer(
        relay, (FileAttachment(source_file_id="f1", name="big.zip", size=340 * MB),)
    )
    assert ids == ("uploaded-big.zip",)
    assert notice == ""


@pytest.mark.asyncio
async def test_업로드_실패는_예외가_아니라_문구가_된다() -> None:
    # ★ 첨부 하나 때문에 본문까지 재시도 큐로 가면 안 된다.
    relay = relay_with(FakeAdapter(), FakeAdapter(uploads_ok=False))
    ids, notice = await transfer(relay, (FileAttachment(source_file_id="f1", name="a.txt"),))
    assert ids == ()
    assert "a.txt" in notice and "전송 실패" in notice


@pytest.mark.asyncio
async def test_업로드_미지원_대상은_문구로_알린다() -> None:
    relay = relay_with(FakeAdapter(), FakeAdapter(supports=False))
    ids, notice = await transfer(relay, (FileAttachment(source_file_id="f1", name="a.txt"),))
    assert ids == ()
    assert "미지원" in notice


@pytest.mark.asyncio
async def test_relay_files_가_꺼져_있어도_사라졌다는_사실은_알린다() -> None:
    relay = relay_with(FakeAdapter(), FakeAdapter())
    ids, notice = await transfer(
        relay, (FileAttachment(source_file_id="f1", name="a.txt"),), relay_files=False
    )
    assert ids == ()
    assert "a.txt" in notice


@pytest.mark.asyncio
async def test_일부만_실패해도_나머지는_전달된다() -> None:
    dst = FakeAdapter(limit=100 * MB)
    relay = relay_with(FakeAdapter(), dst)
    ids, notice = await transfer(
        relay,
        (
            FileAttachment(source_file_id="f1", name="ok.txt", size=10),
            FileAttachment(source_file_id="f2", name="big.zip", size=340 * MB),
        ),
    )
    assert ids == ("uploaded-ok.txt",)
    assert "big.zip" in notice
    assert "ok.txt" not in notice


# --------------------------------------------- 게시 순서 (PRE_UPLOAD / POST_ATTACH)


async def send_via(relay: Relay, files: tuple[FileAttachment, ...], text: str = "본문") -> str:
    from chbridge.cir import Event, EventKind, MessageRef

    event = Event(
        kind=EventKind.MESSAGE_CREATED,
        source=MessageRef(
            platform=Platform.MATTERMOST, workspace_id="src", channel_id="c", message_id="m"
        ),
        event_key="k",
        text=text,
        files=files,
    )
    return await relay._send_with_files(
        event, endpoint("src"), endpoint("dst"), None, relay_files=True
    )


@pytest.mark.asyncio
async def test_PRE_UPLOAD_는_게시에_file_ids_를_싣는다() -> None:
    # ★ 이 검증이 없으면 파일이 올라가고도 게시물에 붙지 않는다.
    #   실제로 리팩터링 중 이 연결이 끊겨 ruff 가 잡아냈다.
    dst = FakeAdapter(mode=FileMode.PRE_UPLOAD)
    relay = relay_with(FakeAdapter(), dst)
    await send_via(relay, (FileAttachment(source_file_id="f1", name="a.txt", size=11),))

    assert len(dst.sent) == 1
    assert dst.sent[0].file_ids == ("uploaded-a.txt",)  # type: ignore[attr-defined]
    assert dst.edited == []  # 편집 없이 한 번에 끝난다


@pytest.mark.asyncio
async def test_POST_ATTACH_는_게시_후_message_id_로_첨부한다() -> None:
    dst = FakeAdapter(mode=FileMode.POST_ATTACH)
    relay = relay_with(FakeAdapter(), dst)
    new_id = await send_via(relay, (FileAttachment(source_file_id="f1", name="a.txt", size=11),))

    assert new_id == "m"
    # 본문에는 file_ids 를 싣지 않는다 (Slack 은 실을 수 없다).
    assert dst.sent[0].file_ids == ()  # type: ignore[attr-defined]
    # 대신 방금 게시한 메시지 id 를 받아 매단다.
    assert dst.attached_to["a.txt"] == "m"
    assert dst.edited == []


@pytest.mark.asyncio
async def test_POST_ATTACH_사전_검사_실패는_본문에_미리_실린다() -> None:
    # 편집 왕복 없이 한 번에 끝나야 한다.
    dst = FakeAdapter(mode=FileMode.POST_ATTACH, limit=1024)
    relay = relay_with(FakeAdapter(), dst)
    await send_via(relay, (FileAttachment(source_file_id="f1", name="big.zip", size=99999),))

    assert "big.zip" in dst.sent[0].text  # type: ignore[attr-defined]
    assert dst.edited == []


@pytest.mark.asyncio
async def test_POST_ATTACH_전송_실패는_게시_후_편집으로_알린다() -> None:
    # 전송 실패는 게시 뒤에야 알 수 있다. 조용히 넘어가지 않는다.
    dst = FakeAdapter(mode=FileMode.POST_ATTACH, uploads_ok=False)
    relay = relay_with(FakeAdapter(), dst)
    await send_via(relay, (FileAttachment(source_file_id="f1", name="a.txt", size=11),))

    assert "a.txt" not in dst.sent[0].text  # type: ignore[attr-defined]
    assert len(dst.edited) == 1
    assert "a.txt" in dst.edited[0].text and "전송 실패" in dst.edited[0].text  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_POST_ATTACH_는_같은_파일을_두_번_안내하지_않는다() -> None:
    dst = FakeAdapter(mode=FileMode.POST_ATTACH, limit=1024)
    relay = relay_with(FakeAdapter(), dst)
    await send_via(relay, (FileAttachment(source_file_id="f1", name="big.zip", size=99999),))

    assert dst.sent[0].text.count("big.zip") == 1  # type: ignore[attr-defined]
    assert dst.edited == []
