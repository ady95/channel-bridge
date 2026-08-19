"""이모지 이름 매핑. (PRD 5.6)

Slack 과 Mattermost 는 기본 이모지 이름이 **대부분** 같다. 둘 다 표준
이모지 shortcode 를 따르기 때문이다. 따라서 기본 동작은 통과(identity)이고,
알려진 불일치만 표로 보정한다.

정직하게 적어둔다: 이 표는 완전하지 않다. 두 플랫폼의 별칭 집합을 전수
비교한 것이 아니라 실무에서 자주 부딪히는 것만 담았다. 매핑 실패는
"조용히 스킵 + 로그"로 처리하므로(PRD 5.6) 표가 불완전해도 브릿지가
멈추지는 않는다.

메시지 본문의 `:shortcode:` 는 변환하지 않는다. 각 플랫폼이 아는 것만
렌더링하고 모르는 것은 텍스트로 남기므로 손실이 없고, 잘못 바꾸면 오히려
원문이 망가진다. 이 모듈은 **리액션 전달**(Phase 3)에서 쓴다.

커스텀 이모지 복제는 명시적 범위 외다 (PRD 7).
"""

from __future__ import annotations

# Slack 이름 -> Mattermost 이름. 양쪽 다 아는 이름은 넣지 않는다.
_SLACK_TO_MM: dict[str, str] = {
    # Slack 고유 별칭
    "simple_smile": "slightly_smiling_face",
    "hankey": "poop",
    "thumbsup_all": "thumbsup",
    # 국기/기호 별칭 차이
    "flag-kr": "kr",
    "flag-us": "us",
    "flag-jp": "jp",
    "flag-cn": "cn",
}

# 역방향. 충돌 시 먼저 등록된 것을 유지한다.
_MM_TO_SLACK: dict[str, str] = {}
for _slack, _mm in _SLACK_TO_MM.items():
    _MM_TO_SLACK.setdefault(_mm, _slack)


def normalize(name: str) -> str:
    """콜론과 스킨톤 수정자를 떼어낸 기본 이름."""
    return name.strip().strip(":").split("::", 1)[0]


def slack_to_mattermost(name: str) -> str:
    base = normalize(name)
    return _SLACK_TO_MM.get(base, base)


def mattermost_to_slack(name: str) -> str:
    base = normalize(name)
    return _MM_TO_SLACK.get(base, base)


def translate(name: str, *, source: str, target: str) -> str:
    """플랫폼 간 이모지 이름 변환.

    source/target 은 Platform 값("mattermost"/"slack")이다.
    같은 플랫폼끼리(MM↔MM)면 변환하지 않는다.
    """
    if source == target:
        return normalize(name)
    if source == "slack" and target == "mattermost":
        return slack_to_mattermost(name)
    if source == "mattermost" and target == "slack":
        return mattermost_to_slack(name)
    return normalize(name)
