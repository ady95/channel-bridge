"""표시 이름 캐시.

이름은 메시지마다 필요하므로 매번 조회할 수 없다. 그렇다고 TTL 없이 캐시하면
사용자가 프로필을 바꿔도 프로세스가 살아 있는 동안 옛 이름으로 계속 전달된다.
초기 구현이 실제로 그랬고, 재기동 전까지 복구되지 않았다.

그래서 두 겹으로 막는다.
  - TTL          : 갱신 이벤트를 놓쳐도 결국 수렴한다. 정확성의 하한선.
  - put()/pop()  : 플랫폼이 프로필 변경 이벤트를 주면 즉시 반영한다.

TTL 만으로도 언젠가는 맞지만, 이름을 바꾼 사람은 즉시 확인하려 하므로
이벤트 반영이 실질적인 사용자 경험을 결정한다. 반대로 이벤트만 믿으면
WebSocket 이 끊긴 구간의 변경을 영원히 놓치므로 TTL 이 필요하다.
"""

from __future__ import annotations

import time

# 기본 TTL. 이름 변경은 드물지만, 놓쳤을 때 5분 이상 틀린 이름이 남는 것은
# 곤란하다. 조회 비용(사용자당 API 1회)이 싸므로 짧게 잡는다.
_DEFAULT_TTL = 300.0

# 워크스페이스 규모를 넘어서면 누수로 본다. 채널 참여자 수만큼만 커지는 것이
# 정상이므로 이 값에 닿을 일은 없다.
_DEFAULT_MAX = 5000


class NameCache:
    """user_id -> 표시 이름. TTL 이 지나면 미적중으로 취급한다."""

    def __init__(self, *, ttl: float = _DEFAULT_TTL, max_size: int = _DEFAULT_MAX) -> None:
        self._ttl = ttl
        self._max_size = max_size
        self._entries: dict[str, tuple[str, float]] = {}

    def get(self, user_id: str) -> str | None:
        entry = self._entries.get(user_id)
        if entry is None:
            return None
        name, stored_at = entry
        if time.monotonic() - stored_at >= self._ttl:
            del self._entries[user_id]
            return None
        return name

    def put(self, user_id: str, name: str) -> None:
        if not user_id:
            return
        # 무한 증식 방지. LRU 를 둘 만큼 크지 않으므로 통째로 비운다.
        if len(self._entries) >= self._max_size and user_id not in self._entries:
            self._entries.clear()
        self._entries[user_id] = (name, time.monotonic())

    def pop(self, user_id: str) -> None:
        self._entries.pop(user_id, None)

    def __len__(self) -> int:
        return len(self._entries)
