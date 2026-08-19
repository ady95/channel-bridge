"""설정 검증과 CIR 직렬화 회귀 테스트."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from chbridge.cir import Author, Event, EventKind, MessageRef, OutboundMessage, Platform
from chbridge.config import AppConfig
from chbridge.store.cursors import _is_newer

# --------------------------------------------------------------- 설정 검증

BRIDGE_ONE: list[dict[str, Any]] = [
    {
        "id": "one",
        "name": "A<->B",
        "endpoints": [
            {"workspace": "mm-a", "team": "t", "channel": "c1"},
            {"workspace": "mm-b", "team": "t", "channel": "c1"},
        ],
    }
]

BASE: dict[str, Any] = {
    "workspaces": [
        {
            "id": "mm-a",
            "platform": "mattermost",
            "alias": "본사MM",
            "base_url": "http://a:8065",
            "token_env": "TOKEN_A",
        },
        {
            "id": "mm-b",
            "platform": "mattermost",
            "alias": "파트너MM",
            "base_url": "http://b:8065",
            "token_env": "TOKEN_B",
        },
        {
            "id": "mm-c",
            "platform": "mattermost",
            "alias": "제3MM",
            "base_url": "http://c:8065",
            "token_env": "TOKEN_C",
        },
    ],
    "bridges": BRIDGE_ONE,
}


def test_정상_설정은_로드된다() -> None:
    config = AppConfig.model_validate(BASE)
    assert len(config.bridges) == 1
    assert config.workspace("mm-a").alias == "본사MM"


def test_채널이_두_브릿지에_속하면_거부된다() -> None:
    """토폴로지 사이클 방지. (PRD 5.1)

    A<->B, B<->C 를 각각 등록하면 A 의 메시지가 C 까지 2홉 전파된다.
    DB 제약으로도 막지만 기동 즉시 알려주는 편이 낫다.
    """
    data = {
        **BASE,
        "bridges": [
            *BRIDGE_ONE,
            {
                "id": "two",
                "name": "B<->C",
                "endpoints": [
                    {"workspace": "mm-b", "team": "t", "channel": "c1"},
                    {"workspace": "mm-c", "team": "t", "channel": "c1"},
                ],
            },
        ],
    }
    with pytest.raises(ValueError, match="중복 등록"):
        AppConfig.model_validate(data)


def test_알_수_없는_워크스페이스는_거부된다() -> None:
    data = {
        **BASE,
        "bridges": [
            {
                "id": "bad",
                "name": "x",
                "endpoints": [
                    {"workspace": "mm-a", "team": "t", "channel": "c9"},
                    {"workspace": "없는워크스페이스", "team": "t", "channel": "c9"},
                ],
            }
        ],
    }
    with pytest.raises(ValueError, match="알 수 없는 워크스페이스"):
        AppConfig.model_validate(data)


def test_엔드포인트가_하나면_거부된다() -> None:
    data = {
        **BASE,
        "bridges": [
            {
                "id": "solo",
                "name": "x",
                "endpoints": [{"workspace": "mm-a", "team": "t", "channel": "c9"}],
            }
        ],
    }
    with pytest.raises(ValueError, match="2개 이상"):
        AppConfig.model_validate(data)


def test_mattermost_는_base_url_이_필요하다() -> None:
    data = {
        "workspaces": [{"id": "x", "platform": "mattermost", "alias": "x", "token_env": "T"}],
        "bridges": [],
    }
    with pytest.raises(ValueError, match="base_url"):
        AppConfig.model_validate(data)


def test_slack_은_app_token_env_가_필요하다() -> None:
    """Socket Mode 는 App-Level Token 이 필수다. (PRD 4.3)"""
    data = {
        "workspaces": [{"id": "s", "platform": "slack", "alias": "s", "token_env": "T"}],
        "bridges": [],
    }
    with pytest.raises(ValueError, match="app_token_env"):
        AppConfig.model_validate(data)


def test_실제_dev_bridges_yaml_이_유효하다(tmp_path: Path) -> None:
    """저장소에 커밋된 개발용 설정이 항상 로드 가능해야 한다."""
    source = Path(__file__).resolve().parents[2] / "dev" / "bridges.yaml"
    if not source.exists():
        pytest.skip("dev/bridges.yaml 없음")
    target = tmp_path / "bridges.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    config = AppConfig.load(target)
    assert config.bridges
    # 자격증명이 YAML 에 직접 들어가지 않았는지 확인 (NFR-3)
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    for ws in raw["workspaces"]:
        assert "token" not in ws, f"{ws['id']}: 토큰 값이 YAML 에 노출됨"


# --------------------------------------------------------------- CIR


def sample_event() -> Event:
    return Event(
        kind=EventKind.MESSAGE_CREATED,
        source=MessageRef(
            platform=Platform.MATTERMOST,
            workspace_id="mm-a",
            channel_id="chan",
            message_id="post-1",
        ),
        event_key="message_created:post-1:123",
        author=Author(platform_user_id="u1", display_name="Alice Kim"),
        text="안녕하세요 :wave:",
        parent_message_id="root-1",
        created_at_ms=1786110929051,
        cursor="1786110929051",
        raw={"nested": {"value": 1}},
    )


def test_이벤트는_json_왕복에서_보존된다() -> None:
    """event_inbox 의 JSONB 왕복이 손실 없이 되어야 한다. (PRD 5.9)"""
    original = sample_event()
    restored = Event.from_json(original.to_json())
    assert restored == original


def test_messageref_는_workspace_를_키에_포함한다() -> None:
    """MM↔MM 회귀 방지. 서버가 달라도 channel_id 가 겹칠 수 있다."""
    a = MessageRef(
        platform=Platform.MATTERMOST, workspace_id="mm-a", channel_id="c", message_id="m"
    )
    b = MessageRef(
        platform=Platform.MATTERMOST, workspace_id="mm-b", channel_id="c", message_id="m"
    )
    assert a.key() != b.key()
    assert a != b


def test_표시이름에_출처_별칭이_붙는다() -> None:
    msg = OutboundMessage(
        text="x",
        author=Author(platform_user_id="u", display_name="Alice Kim"),
        source_alias="본사MM",
    )
    assert msg.display_name() == "Alice Kim (본사MM)"


def test_별칭이_없으면_이름만_쓴다() -> None:
    msg = OutboundMessage(text="x", author=Author(platform_user_id="u", display_name="Alice Kim"))
    assert msg.display_name() == "Alice Kim"


def test_작성자가_없으면_표시이름도_없다() -> None:
    assert OutboundMessage(text="x", author=None).display_name() is None


def test_쓰레드_판정() -> None:
    assert sample_event().is_thread_reply()
    assert not sample_event().model_copy(update={"parent_message_id": None}).is_thread_reply()


# --------------------------------------------------------------- 커서


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("1786110929051", "1786110929050", True),
        ("1786110929050", "1786110929051", False),
        ("1786110929051", "1786110929051", False),
        # Slack ts 형식
        ("1786107692.046269", "1786107692.046268", True),
        ("1786107692.046268", "1786107692.046269", False),
        # 수치 변환 실패 시 문자열 비교로 낙착
        ("b", "a", True),
        ("a", "b", False),
    ],
)
def test_커서는_뒤로_가지_않는다(candidate: str, current: str, expected: bool) -> None:
    """커서가 뒤로 밀리면 재연결 때 처리한 구간을 다시 긁는다. (PRD 5.9)"""
    assert _is_newer(candidate, current) is expected
