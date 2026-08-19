"""Slack mrkdwn <-> Markdown 변환. (PRD 5.4)

CIR 의 text 는 **표준 Markdown** 을 정본으로 삼는다. Mattermost 는 거의
그대로 쓰고, Slack 어댑터가 수신 시 mrkdwn -> Markdown, 송신 시 Markdown ->
mrkdwn 으로 양쪽에서 변환한다. 이러면 플랫폼이 늘어도 변환이 N x N 으로
번지지 않는다.

두 가지가 특히 어긋난다.
  - 굵게: Slack `*bold*`  vs  Markdown `**bold**`
    Slack 의 별표 하나가 Markdown 에서는 기울임이라 그냥 통과시키면
    의미가 바뀐다.
  - 링크: Slack `<url|text>`  vs  Markdown `[text](url)`
    Slack 은 `&`, `<`, `>` 를 HTML 엔티티로 이스케이프한다.

기울임은 텍스트 수준 왕복이 원리적으로 불가능하다. Markdown 은 `*x*` 와
`_x_` 두 표기를 쓰는데 Slack 은 `_x_` 하나뿐이므로, 돌아올 때 원래 표기를
복원할 수 없다. **`_x_` 를 정본으로 삼아 정규화한다.** 의미는 보존된다.

코드 블록/인라인 코드 안은 절대 변환하지 않는다. 사용자가 붙여넣은 코드가
망가지면 브릿지를 쓸 수 없다. 그래서 먼저 코드 구간을 뽑아내 자리표시자로
치환하고, 변환이 끝난 뒤 되돌린다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# ---------------------------------------------------------------- 코드 보호

# 자리표시자는 사용자 입력에 나타날 수 없는 제어문자를 쓴다.
_SENTINEL = "\x00"
_BOLD_MARK = "\x01"
_STRIKE_MARK = "\x02"

_CODE_SPANS = re.compile(
    r"```.*?```"  # 코드 블록 (여러 줄)
    r"|`[^`\n]+`",  # 인라인 코드
    re.DOTALL,
)


def _protect(text: str) -> tuple[str, list[str]]:
    """코드 구간을 자리표시자로 치환한다."""
    saved: list[str] = []

    def take(match: re.Match[str]) -> str:
        saved.append(match.group(0))
        return f"{_SENTINEL}{len(saved) - 1}{_SENTINEL}"

    return _CODE_SPANS.sub(take, text), saved


def _restore(text: str, saved: list[str]) -> str:
    for index, original in enumerate(saved):
        text = text.replace(f"{_SENTINEL}{index}{_SENTINEL}", original)
    return text


# ---------------------------------------------------------------- Slack -> Markdown

# <url|label> / <url> / <@U123|name> / <#C123|name> / <!here> ...
_SLACK_ENTITY = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")


def slack_to_markdown(text: str, *, users: Mapping[str, str] | None = None) -> str:
    """Slack mrkdwn 을 Markdown 으로 변환한다.

    users: Slack user id -> 표시 이름. `<@U123>` 처럼 라벨이 없는 멘션을
           사람이 읽는 이름으로 바꾸는 데 쓴다. 없으면 id 를 그대로 둔다.
    """
    if not text:
        return text

    body, saved = _protect(text)
    lookup = users or {}

    def entity(match: re.Match[str]) -> str:
        target, label = match.group(1), match.group(2)

        # 사용자 멘션. v1 은 표시용 텍스트로만 바꾼다 (FR-7.1).
        if target.startswith("@"):
            uid = target[1:]
            return f"@{label or lookup.get(uid, uid)}"

        # 채널 멘션
        if target.startswith("#"):
            cid = target[1:]
            return f"#{label or cid}"

        # 특수 멘션 (@here / @channel / @everyone / 사용자 그룹)
        if target.startswith("!"):
            special = target[1:]
            if special.startswith("subteam^"):
                return label or "@group"
            if special in ("here", "channel", "everyone"):
                return f"@{special}"
            return label or f"@{special}"

        # 일반 링크. 라벨이 URL 과 같으면 자동 링크이므로 URL 만 남긴다.
        url = target
        if label and label != url:
            return f"[{label}]({url})"
        return url

    body = _SLACK_ENTITY.sub(entity, body)

    # 취소선: ~x~ -> ~~x~~   (굵게보다 먼저. 물결이 하나뿐인 쪽을 먼저 잡는다)
    body = re.sub(r"(?<![~\w])~(?!\s)([^~\n]+?)(?<!\s)~(?![~\w])", r"~~\1~~", body)

    # 굵게: *x* -> **x**
    # 앞뒤가 단어문자면 곱셈·와일드카드일 수 있으므로 건드리지 않는다.
    body = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])", r"**\1**", body)

    # 기울임 `_x_` 는 Mattermost 도 동일하게 해석하므로 손대지 않는다.

    # 링크 파싱이 끝난 뒤에 엔티티를 되돌린다.
    # (먼저 풀면 &lt; 가 < 로 바뀌어 링크 문법과 뒤섞인다)
    body = body.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

    return _restore(body, saved)


# ---------------------------------------------------------------- Markdown -> Slack

_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$", re.MULTILINE)


def markdown_to_slack(text: str) -> str:
    """Markdown 을 Slack mrkdwn 으로 변환한다."""
    if not text:
        return text

    body, saved = _protect(text)

    # Slack 은 &, <, > 를 엔티티로 받는다. 링크 문법(<url|label>)을 만들기
    # 전에 이스케이프해야 사용자가 쓴 부등호와 섞이지 않는다.
    body = body.replace("&", "&amp;").replace("<", "&lt;")
    # '>' 는 행 머리에서 인용문이므로 그 경우만 남긴다.
    body = re.sub(r"(?<!\n)>", "&gt;", body)
    body = re.sub(r"^&gt;", ">", body, flags=re.MULTILINE)

    # 제목: Slack 에는 제목 문법이 없다. 굵게로 격하한다.
    body = _MD_HEADING.sub(lambda m: f"{_BOLD_MARK}{m.group(2)}{_BOLD_MARK}", body)

    # 링크: [label](url) -> <url|label>
    body = _MD_LINK.sub(
        lambda m: f"<{m.group(2)}|{m.group(1)}>" if m.group(1) else f"<{m.group(2)}>",
        body,
    )

    # 굵게를 먼저 자리표시자로 뺀다. 그래야 남은 별표 하나를 기울임으로
    # 안전하게 판정할 수 있다.
    body = re.sub(r"\*\*(?!\s)([^\n]+?)(?<!\s)\*\*", rf"{_BOLD_MARK}\1{_BOLD_MARK}", body)
    body = re.sub(r"__(?!\s)([^\n]+?)(?<!\s)__", rf"{_BOLD_MARK}\1{_BOLD_MARK}", body)

    # 취소선 ~~x~~
    body = re.sub(r"~~(?!\s)([^\n]+?)(?<!\s)~~", rf"{_STRIKE_MARK}\1{_STRIKE_MARK}", body)

    # 남은 별표 하나는 기울임 -> Slack 은 밑줄을 쓴다
    body = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])", r"_\1_", body)

    # 자리표시자를 Slack 문법으로 되돌린다
    body = body.replace(_BOLD_MARK, "*").replace(_STRIKE_MARK, "~")

    return _restore(body, saved)


# ---------------------------------------------------------------- 멘션 추출

_MENTION_IDS = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^<>]*)?>")


def slack_mention_ids(text: str) -> set[str]:
    """텍스트에 등장하는 Slack 사용자 id 목록.

    어댑터가 이름을 미리 조회해 slack_to_markdown 에 넘기기 위한 것이다.
    """
    return set(_MENTION_IDS.findall(text or ""))
