"""Slack 이벤트 -> CIR 변환 회귀 테스트.

★ 이 파일이 막는 것: **subtype 마다 bot_id 위치가 다르다**는 사실을 놓쳐서
  자기 발신을 걸러내지 못하고 루프가 발생하는 것. (NFR-2 무관용 지표)

    (없음), bot_message  -> 최상위 bot_id
    message_changed      -> message.bot_id
    message_deleted      -> previous_message.bot_id

Phase 0 실측에서 확인한 구조를 합성 페이로드로 고정한다. 라이브 검증에
의존하지 않는 이유: Slack 은 앱이 자기 메시지를 chat.delete 로 지울 때
message_deleted 이벤트를 항상 보내지는 않아서, 통합 테스트로는 이 경로가
우연히 실행되지 않을 수 있다.
"""

from __future__ import annotations

import pytest

from chbridge.adapters.slack import SlackAdapter
from chbridge.cir import EventKind

BOT_ID = "B0BNTK60MT3"
APP_ID = "A0BNTK76UE5"
CHANNEL = "C0BNRNCSUUA"


@pytest.fixture
def adapter() -> SlackAdapter:
    a = SlackAdapter(workspace_id="slack-dev", bot_token="xoxb-test", app_token="xapp-test")
    # open() 없이 자기 식별자를 주입한다 (네트워크 접근 회피)
    a._bot_id = BOT_ID
    # 사용자 이름 캐시를 미리 채워 users.info 호출을 막는다
    a._user_names.put("U111", "Alice Kim")
    return a


def envelope(event: dict[str, object], event_id: str = "Ev123") -> dict[str, object]:
    return {"event_id": event_id, "event": event}


# --------------------------------------------------------------- 신규 메시지


async def test_사람이_보낸_메시지(adapter: SlackAdapter) -> None:
    ev = await adapter._to_cir(
        envelope(
            {
                "type": "message",
                "channel": CHANNEL,
                "user": "U111",
                "text": "안녕 *굵게*",
                "ts": "1786114472.056559",
                "event_ts": "1786114472.056559",
            }
        )
    )
    assert ev is not None
    assert ev.kind is EventKind.MESSAGE_CREATED
    assert ev.source.message_id == "1786114472.056559"
    assert ev.author is not None
    assert ev.author.platform_user_id == "U111"
    assert ev.author.display_name == "Alice Kim"
    assert ev.author.is_bot is False
    # mrkdwn -> Markdown 변환이 적용된다
    assert ev.text == "안녕 **굵게**"
    # Slack 이 주는 event_id 를 멱등 키로 쓴다
    assert ev.event_key == "Ev123"


async def test_봇이_보낸_메시지는_최상위_bot_id로_식별된다(adapter: SlackAdapter) -> None:
    """오버라이드 게시 시 user 필드가 비고 bot_id 만 남는다. (Phase 0 실측)"""
    ev = await adapter._to_cir(
        envelope(
            {
                "type": "message",
                "subtype": "bot_message",
                "channel": CHANNEL,
                "bot_id": BOT_ID,
                "app_id": APP_ID,
                "username": "Alice Kim (본사MM)",
                "text": "복제된 메시지",
                "ts": "1786114472.056559",
            }
        )
    )
    assert ev is not None
    assert ev.author is not None
    # ★ self_id() 와 비교 가능한 값이어야 LoopGuard 가 걸러낼 수 있다
    assert ev.author.platform_user_id == adapter.self_id()
    assert ev.author.is_bot is True


# --------------------------------------------------------------- 편집


async def test_편집은_message_bot_id로_식별된다(adapter: SlackAdapter) -> None:
    """★ 최상위가 아니라 message.bot_id 다. 놓치면 편집이 루프한다."""
    ev = await adapter._to_cir(
        envelope(
            {
                "type": "message",
                "subtype": "message_changed",
                "channel": CHANNEL,
                "message": {
                    "type": "message",
                    "subtype": "bot_message",
                    "bot_id": BOT_ID,
                    "username": "Alice Kim (본사MM)",
                    "text": "수정된 내용",
                    "ts": "1786114472.056559",
                },
                "previous_message": {"text": "이전 내용", "ts": "1786114472.056559"},
                "event_ts": "1786114484.000100",
            }
        )
    )
    assert ev is not None
    assert ev.kind is EventKind.MESSAGE_EDITED
    assert ev.source.message_id == "1786114472.056559"
    assert ev.text == "수정된 내용"
    assert ev.author is not None
    assert ev.author.platform_user_id == adapter.self_id(), (
        "편집 이벤트의 bot_id 를 message 에서 읽지 않으면 루프가 발생한다"
    )


# --------------------------------------------------------------- 삭제


async def test_삭제는_previous_message_bot_id로_식별된다(adapter: SlackAdapter) -> None:
    """★ 삭제 이벤트에는 message 키가 없다. previous_message 를 봐야 한다."""
    ev = await adapter._to_cir(
        envelope(
            {
                "type": "message",
                "subtype": "message_deleted",
                "channel": CHANNEL,
                "deleted_ts": "1786114472.056559",
                "previous_message": {
                    "type": "message",
                    "subtype": "bot_message",
                    "bot_id": BOT_ID,
                    "username": "Alice Kim (본사MM)",
                    "text": "지워질 내용",
                    "ts": "1786114472.056559",
                },
                "event_ts": "1786114486.000200",
            }
        )
    )
    assert ev is not None
    assert ev.kind is EventKind.MESSAGE_DELETED
    assert ev.source.message_id == "1786114472.056559"
    assert ev.author is not None
    assert ev.author.platform_user_id == adapter.self_id(), (
        "삭제 이벤트의 bot_id 를 previous_message 에서 읽지 않으면 루프가 발생한다"
    )


# --------------------------------------------------------------- 쓰레드


async def test_쓰레드_답글은_부모_ts를_갖는다(adapter: SlackAdapter) -> None:
    ev = await adapter._to_cir(
        envelope(
            {
                "type": "message",
                "channel": CHANNEL,
                "user": "U111",
                "text": "답글",
                "ts": "1786114482.946639",
                "thread_ts": "1786114472.056559",
            }
        )
    )
    assert ev is not None
    assert ev.parent_message_id == "1786114472.056559"
    assert ev.is_thread_reply()


async def test_쓰레드_부모_자신은_답글이_아니다(adapter: SlackAdapter) -> None:
    """부모는 thread_ts == ts 다. 이를 부모로 오인하면 자기 참조가 된다."""
    ev = await adapter._to_cir(
        envelope(
            {
                "type": "message",
                "channel": CHANNEL,
                "user": "U111",
                "text": "부모",
                "ts": "1786114472.056559",
                "thread_ts": "1786114472.056559",
            }
        )
    )
    assert ev is not None
    assert ev.parent_message_id is None
    assert not ev.is_thread_reply()


# --------------------------------------------------------------- 시스템 메시지


@pytest.mark.parametrize(
    "subtype", ["channel_join", "channel_leave", "channel_topic", "channel_purpose"]
)
async def test_시스템_메시지는_전달하지_않는다(adapter: SlackAdapter, subtype: str) -> None:
    """PRD 2.5 - 입퇴장·채널명 변경 등은 전달 대상이 아니다."""
    ev = await adapter._to_cir(
        envelope(
            {
                "type": "message",
                "subtype": subtype,
                "channel": CHANNEL,
                "user": "U111",
                "text": "<@U111> has joined the channel",
                "ts": "1786114472.056559",
            }
        )
    )
    assert ev is None


async def test_알_수_없는_이벤트는_무시된다(adapter: SlackAdapter) -> None:
    assert await adapter._to_cir(envelope({"type": "team_join"})) is None


# --------------------------------------------------------------- 리액션


async def test_리액션_이벤트(adapter: SlackAdapter) -> None:
    ev = await adapter._to_cir(
        envelope(
            {
                "type": "reaction_added",
                "user": "U111",
                "reaction": "thumbsup",
                "item": {"type": "message", "channel": CHANNEL, "ts": "1786114472.056559"},
                "event_ts": "1786114490.000300",
            }
        )
    )
    assert ev is not None
    assert ev.kind is EventKind.REACTION_ADDED
    assert ev.emoji == "thumbsup"
    assert ev.reactor_id == "U111"
    assert ev.source.message_id == "1786114472.056559"


async def test_메시지가_아닌_대상의_리액션은_무시된다(adapter: SlackAdapter) -> None:
    ev = await adapter._to_cir(
        envelope(
            {
                "type": "reaction_added",
                "user": "U111",
                "reaction": "thumbsup",
                "item": {"type": "file", "file": "F123"},
            }
        )
    )
    assert ev is None
