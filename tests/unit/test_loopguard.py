"""LoopGuard 회귀 테스트.

NFR-2 의 "루프 발생 0건"은 무관용 지표이고, 루프는 리팩터링 중 재발하기 가장
쉬운 결함이다. 판정 규칙을 여기에 고정한다.

Phase 0 실측 근거: Mattermost 도 Slack 도 봇이 자기 발신 이벤트를 받는다.
따라서 방어 ①(자기 발신 무시)이 없으면 첫 전달 즉시 무한 증식한다.
"""

from __future__ import annotations

from typing import cast

import pytest

from chbridge.cir import Author, Event, EventKind, MessageRef, Platform
from chbridge.loopguard import DropReason, LoopGuard
from chbridge.store.links import MessageLinkStore

BOT_A = "bot-user-a"
HUMAN = "human-user-1"


class FakeLinks:
    """MessageLinkStore 스텁. is_replica 만 쓴다."""

    def __init__(self, replicas: set[tuple[str, str, str, str]] | None = None) -> None:
        self.replicas = replicas or set()
        self.calls = 0

    async def is_replica(self, ref: MessageRef) -> bool:
        self.calls += 1
        return ref.key() in self.replicas


def make_event(
    *,
    user_id: str = HUMAN,
    is_bot: bool = False,
    workspace: str = "mm-a",
    message_id: str = "post-1",
    kind: EventKind = EventKind.MESSAGE_CREATED,
) -> Event:
    return Event(
        kind=kind,
        source=MessageRef(
            platform=Platform.MATTERMOST,
            workspace_id=workspace,
            channel_id="chan-1",
            message_id=message_id,
        ),
        event_key=f"{kind.value}:{message_id}",
        author=Author(platform_user_id=user_id, display_name="누군가", is_bot=is_bot),
        text="안녕",
    )


def guard(links: FakeLinks) -> LoopGuard:
    return LoopGuard(cast(MessageLinkStore, links), {"mm-a": BOT_A})


async def test_사람이_보낸_새_메시지는_통과한다() -> None:
    links = FakeLinks()
    assert await guard(links).evaluate(make_event(), relay_bot_messages=False) is None


async def test_자기_발신은_방어1에서_차단된다() -> None:
    """가장 중요한 케이스. 이것이 뚫리면 무한 루프가 발생한다."""
    links = FakeLinks()
    reason = await guard(links).evaluate(
        make_event(user_id=BOT_A, is_bot=True), relay_bot_messages=True
    )
    assert reason is DropReason.SELF_SENT
    # 방어 ①은 DB 왕복 없이 차단해야 한다 (핫패스 비용)
    assert links.calls == 0, "자기 발신은 매핑 조회 전에 걸러져야 한다"


async def test_복제본은_방어2에서_차단된다() -> None:
    """사람이 복제 메시지를 직접 편집한 경우. 작성자는 사람이지만
    대상은 Replica 이므로 원본으로 역전파해선 안 된다. (FR-6.3)"""
    ref = make_event(message_id="replica-1").source
    links = FakeLinks({ref.key()})
    reason = await guard(links).evaluate(
        make_event(message_id="replica-1"), relay_bot_messages=False
    )
    assert reason is DropReason.IS_REPLICA


async def test_다른_봇_메시지는_옵션에_따른다() -> None:
    other_bot = make_event(user_id="other-bot", is_bot=True)
    assert (
        await guard(FakeLinks()).evaluate(other_bot, relay_bot_messages=False)
        is DropReason.BOT_MESSAGE
    )
    assert await guard(FakeLinks()).evaluate(other_bot, relay_bot_messages=True) is None


async def test_다른_워크스페이스의_같은_id_는_자기발신이_아니다() -> None:
    """MM↔MM 회귀 방지.

    두 서버가 모두 mattermost 이므로 workspace 를 구분하지 않으면
    상대 서버의 봇을 자기 자신으로 오인해 전달을 멈춘다.
    """
    event = make_event(user_id=BOT_A, workspace="mm-b", is_bot=True)
    reason = await guard(FakeLinks()).evaluate(event, relay_bot_messages=True)
    assert reason is not DropReason.SELF_SENT


async def test_작성자가_없는_이벤트는_통과한다() -> None:
    """삭제 이벤트 등은 작성자 정보가 없을 수 있다."""
    event = Event(
        kind=EventKind.MESSAGE_DELETED,
        source=MessageRef(
            platform=Platform.MATTERMOST,
            workspace_id="mm-a",
            channel_id="chan-1",
            message_id="post-9",
        ),
        event_key="message_deleted:post-9",
    )
    assert await guard(FakeLinks()).evaluate(event, relay_bot_messages=False) is None


@pytest.mark.parametrize("workspace", ["mm-a", "mm-b", "unknown-ws"])
async def test_식별자_미등록_워크스페이스는_자기발신_판정을_건너뛴다(workspace: str) -> None:
    """자기 식별자를 모르는 워크스페이스는 방어 ②에 의존한다."""
    links = FakeLinks()
    event = make_event(user_id="somebody", workspace=workspace)
    assert await guard(links).evaluate(event, relay_bot_messages=False) is None
    assert links.calls == 1
