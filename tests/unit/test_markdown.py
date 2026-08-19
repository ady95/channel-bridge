"""포맷 변환 회귀 테스트. (PRD 5.4)

사용자가 붙여넣은 텍스트가 브릿지를 통과하며 망가지면 브릿지를 쓸 수 없다.
특히 코드 블록 보호와 굵게/기울임 구분은 조용히 깨지기 쉬워 고정해둔다.
"""

from __future__ import annotations

import pytest

from chbridge.transform.markdown import (
    markdown_to_slack,
    slack_mention_ids,
    slack_to_markdown,
)

# --------------------------------------------------------------- Slack -> Markdown


@pytest.mark.parametrize(
    ("mrkdwn", "expected"),
    [
        ("*굵게*", "**굵게**"),
        ("~취소선~", "~~취소선~~"),
        ("_기울임_", "_기울임_"),  # Mattermost 도 같은 문법이라 통과
        ("평범한 문장", "평범한 문장"),
        # 링크
        ("<https://a.com|사이트>", "[사이트](https://a.com)"),
        ("<https://a.com>", "https://a.com"),
        ("<https://a.com|https://a.com>", "https://a.com"),
        # 멘션
        ("<@U123|alice> 안녕", "@alice 안녕"),
        ("<#C123|general> 참고", "#general 참고"),
        ("<!here> 확인", "@here 확인"),
        ("<!channel>", "@channel"),
        ("<!subteam^S123|@backend>", "@backend"),
        # HTML 엔티티
        ("a &amp; b", "a & b"),
        ("&lt;태그&gt;", "<태그>"),
        # 곱셈 기호를 굵게로 오인하지 않는다
        ("2*3*4", "2*3*4"),
        # 복합
        (
            "*굵게* 그리고 ~취소~ 그리고 <https://x.io|링크>",
            "**굵게** 그리고 ~~취소~~ 그리고 [링크](https://x.io)",
        ),
    ],
)
def test_slack에서_markdown으로(mrkdwn: str, expected: str) -> None:
    assert slack_to_markdown(mrkdwn) == expected


def test_라벨_없는_멘션은_이름을_조회해_바꾼다() -> None:
    assert slack_to_markdown("<@U999> 님", users={"U999": "Alice Kim"}) == "@Alice Kim 님"


def test_이름을_모르면_id를_남긴다() -> None:
    assert slack_to_markdown("<@U999>") == "@U999"


# --------------------------------------------------------------- Markdown -> Slack


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("**굵게**", "*굵게*"),
        ("__굵게__", "*굵게*"),
        ("~~취소선~~", "~취소선~"),
        ("*기울임*", "_기울임_"),
        ("[사이트](https://a.com)", "<https://a.com|사이트>"),
        # 제목은 Slack 에 없으므로 굵게로 격하
        ("# 제목", "*제목*"),
        ("### 소제목", "*소제목*"),
        # 이스케이프
        ("a & b", "a &amp; b"),
        ("2 < 3", "2 &lt; 3"),
        ("3 > 2", "3 &gt; 2"),
        # 행 머리의 > 는 인용문이므로 보존
        ("> 인용문", "> 인용문"),
        # 굵게와 기울임이 섞여도 구분된다
        ("**굵게** 와 *기울임*", "*굵게* 와 _기울임_"),
    ],
)
def test_markdown에서_slack으로(markdown: str, expected: str) -> None:
    assert markdown_to_slack(markdown) == expected


# --------------------------------------------------------------- 코드 보호


def test_인라인_코드_안은_변환하지_않는다() -> None:
    assert markdown_to_slack("`**굵게아님**`") == "`**굵게아님**`"
    assert slack_to_markdown("`*굵게아님*`") == "`*굵게아님*`"


def test_코드블록_안은_변환하지_않는다() -> None:
    block = "```\n**bold** [x](y) a & b\n```"
    assert markdown_to_slack(block) == block
    slack_block = "```\n*bold* <https://a|b> &amp;\n```"
    assert slack_to_markdown(slack_block) == slack_block


def test_코드블록_밖은_변환된다() -> None:
    src = "앞 **굵게**\n```\n**코드**\n```\n뒤 **굵게**"
    out = markdown_to_slack(src)
    assert "앞 *굵게*" in out
    assert "뒤 *굵게*" in out
    assert "**코드**" in out, "코드 블록 내용은 보존돼야 한다"


def test_여러_코드구간이_섞여도_순서가_유지된다() -> None:
    src = "`a` **b** `c` **d**"
    assert markdown_to_slack(src) == "`a` *b* `c` *d*"


# --------------------------------------------------------------- 왕복


@pytest.mark.parametrize(
    "markdown",
    [
        "**굵게**",
        "~~취소선~~",
        "_기울임_",
        "[사이트](https://a.com)",
        "평범한 문장입니다",
        "`인라인 코드`",
        "```\n코드 블록\n```",
        "**굵게** 와 `코드` 와 [링크](https://x.io)",
    ],
)
def test_markdown_왕복에서_의미가_보존된다(markdown: str) -> None:
    """Markdown -> Slack -> Markdown 왕복.

    브릿지 양쪽에 같은 메시지가 오래 살아 있으므로 왕복 안정성이 중요하다.
    """
    assert slack_to_markdown(markdown_to_slack(markdown)) == markdown


def test_기울임_표기는_밑줄로_정규화된다() -> None:
    """원리적 제약이라 버그가 아니다.

    Markdown 은 기울임 표기가 `*x*` 와 `_x_` 두 가지인데 Slack 은 `_x_`
    하나뿐이다. 따라서 Slack 을 거쳐 돌아오면 원래 어느 표기였는지 알 수 없다.
    `_x_` 를 정본으로 삼아 `*x*` 는 그쪽으로 정규화한다. 의미는 동일하다.
    """
    assert markdown_to_slack("*기울임*") == "_기울임_"
    assert slack_to_markdown("_기울임_") == "_기울임_"
    # 정본 표기는 왕복이 안정적이다
    assert slack_to_markdown(markdown_to_slack("_기울임_")) == "_기울임_"


def test_빈_문자열은_그대로() -> None:
    assert markdown_to_slack("") == ""
    assert slack_to_markdown("") == ""


# --------------------------------------------------------------- 멘션 추출


def test_멘션_id_추출() -> None:
    text = "<@U111> 과 <@W222|bob> 과 <#C333|general>"
    assert slack_mention_ids(text) == {"U111", "W222"}


def test_멘션이_없으면_빈_집합() -> None:
    assert slack_mention_ids("멘션 없음") == set()
