"""표시 이름 캐시 회귀 테스트.

★ 이 파일이 막는 것: 사용자가 프로필 이름을 바꿨는데 **재기동 전까지 옛 이름으로
  계속 전달되는 것.** 초기 구현은 TTL 없는 dict 캐시였고, 이름 변경이 영원히
  반영되지 않았다.

두 경로를 각각 고정한다.
  - 프로필 변경 이벤트를 받으면 즉시 반영된다.
  - 이벤트를 놓쳐도 TTL 이 지나면 재조회한다.
"""

from __future__ import annotations

import pytest

from chbridge.adapters.mattermost import MattermostAdapter
from chbridge.adapters.names import NameCache
from chbridge.adapters.slack import SlackAdapter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr("chbridge.adapters.names.time.monotonic", c)
    return c


# ------------------------------------------------------------------ NameCache


def test_TTL_이내에는_캐시가_적중한다(clock: FakeClock) -> None:
    cache = NameCache(ttl=300.0)
    cache.put("u1", "Alice Kim")
    clock.now += 299.0
    assert cache.get("u1") == "Alice Kim"


def test_TTL_이_지나면_미적중이_된다(clock: FakeClock) -> None:
    cache = NameCache(ttl=300.0)
    cache.put("u1", "Alice Kim")
    clock.now += 301.0
    assert cache.get("u1") is None


def test_모르는_사용자는_미적중이다() -> None:
    assert NameCache().get("없음") is None


def test_상한을_넘으면_비우고_새로_담는다() -> None:
    cache = NameCache(max_size=3)
    for i in range(3):
        cache.put(f"u{i}", f"name{i}")
    assert len(cache) == 3
    cache.put("u9", "새 사람")
    # 통째로 비운 뒤 새 항목만 남는다. 무한 증식하지 않는 것이 요점이다.
    assert len(cache) == 1
    assert cache.get("u9") == "새 사람"


def test_기존_키_갱신은_상한을_건드리지_않는다() -> None:
    cache = NameCache(max_size=3)
    for i in range(3):
        cache.put(f"u{i}", f"name{i}")
    cache.put("u0", "바뀐 이름")
    assert len(cache) == 3
    assert cache.get("u0") == "바뀐 이름"


# ------------------------------------------------- 플랫폼 이벤트로 즉시 갱신


@pytest.mark.asyncio
async def test_MM_user_updated_는_캐시를_즉시_갱신한다() -> None:
    adapter = MattermostAdapter(workspace_id="mm-a", base_url="http://localhost:8065", token="t")
    adapter._user_names.put("uid1", "옛 이름")

    event = await adapter._to_cir(
        {
            "event": "user_updated",
            "data": {"user": {"id": "uid1", "nickname": "새 이름", "username": "alice"}},
        }
    )

    # 전달 대상 이벤트는 아니다. 캐시만 갱신한다.
    assert event is None
    assert adapter._user_names.get("uid1") == "새 이름"


@pytest.mark.asyncio
async def test_MM_user_updated_는_nickname_없으면_이름_성을_쓴다() -> None:
    adapter = MattermostAdapter(workspace_id="mm-a", base_url="http://localhost:8065", token="t")
    await adapter._to_cir(
        {
            "event": "user_updated",
            "data": {
                "user": {
                    "id": "uid1",
                    "nickname": "",
                    "first_name": "Alice",
                    "last_name": "Kim",
                    "username": "alice",
                }
            },
        }
    )
    assert adapter._user_names.get("uid1") == "Alice Kim"


@pytest.mark.asyncio
async def test_Slack_user_change_는_캐시를_즉시_갱신한다() -> None:
    adapter = SlackAdapter(workspace_id="slack-dev", bot_token="xoxb", app_token="xapp")
    adapter._user_names.put("U111", "옛 이름")

    event = await adapter._to_cir(
        {
            "event_id": "Ev1",
            "event": {
                "type": "user_change",
                "user": {"id": "U111", "profile": {"display_name": "새 이름"}},
            },
        }
    )

    assert event is None
    assert adapter._user_names.get("U111") == "새 이름"


@pytest.mark.asyncio
async def test_망가진_user_페이로드는_무시한다() -> None:
    adapter = MattermostAdapter(workspace_id="mm-a", base_url="http://localhost:8065", token="t")
    for payload in ({"user": None}, {"user": {}}, {}):
        assert await adapter._to_cir({"event": "user_updated", "data": payload}) is None
    assert len(adapter._user_names) == 0
