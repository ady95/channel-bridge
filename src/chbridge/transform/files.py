"""전달되지 않은 첨부의 안내 문구.

★ 이 모듈이 존재하는 이유: 첨부가 **조용히 사라지는 것**을 막기 위해서다.
초기 구현은 텍스트만 전달하고 파일을 버렸는데, 받는 쪽에서는 애초에 첨부가
있었다는 사실조차 알 수 없었다. 원본을 다시 열어보기 전까지 유실을 발견할
방법이 없다는 것이 문제의 핵심이었다.

그래서 전달하지 못한 첨부는 이유와 함께 본문에 남긴다. 링크 대체나 외부
저장소는 그 다음 문제다. 먼저 "빠졌다는 사실"이 보여야 한다.
"""

from __future__ import annotations

from enum import StrEnum

from chbridge.cir import FileAttachment

_UNITS = ("B", "KB", "MB", "GB", "TB")


class SkipReason(StrEnum):
    TOO_LARGE = "too_large"
    RELAY_DISABLED = "relay_disabled"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


def human_size(size: int) -> str:
    """사람이 읽는 크기. 안내 문구에서 숫자가 바로 이해돼야 한다."""
    if size <= 0:
        return "크기 미상"
    value = float(size)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}{_UNITS[-1]}"


def _explain(reason: SkipReason, limit: int) -> str:
    match reason:
        case SkipReason.TOO_LARGE:
            return f"대상의 크기 제한({human_size(limit)}) 초과"
        case SkipReason.RELAY_DISABLED:
            return "이 브릿지는 파일 전달이 꺼져 있음"
        case SkipReason.UNSUPPORTED:
            return "대상 플랫폼 업로드 미지원"
        case SkipReason.FAILED:
            return "전송 실패"


def skip_notice(skipped: list[tuple[FileAttachment, SkipReason]], *, limit: int = 0) -> str:
    """전달되지 않은 첨부 안내. 없으면 빈 문자열."""
    if not skipped:
        return ""
    lines = [f"📎 전달되지 않은 첨부 {len(skipped)}개"]
    lines += [
        f"• {file.name} ({human_size(file.size)}) — {_explain(reason, limit)}"
        for file, reason in skipped
    ]
    # 이탤릭으로 감싸 원문과 시각적으로 구분한다. 양 플랫폼 공통 문법이다.
    return "\n".join(f"_{line}_" for line in lines)


def append_notice(text: str, notice: str) -> str:
    if not notice:
        return text
    return f"{text}\n\n{notice}" if text else notice
