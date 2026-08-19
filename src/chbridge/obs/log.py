"""구조화 로깅. (FR-9.3)

`bridge_id`, `group_id`, `event_key` 로 추적 가능해야 한다. 브릿지 장애는
"어느 메시지가 어디서 멈췄는가"를 재구성하는 문제이므로, 사람이 읽는 문장이
아니라 검색 가능한 필드가 중요하다.

개발 중에는 사람이 읽는 컬러 출력, 배포 시에는 JSON 한 줄로 낸다.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure(*, level: str = "INFO", json_output: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    # 서드파티 라이브러리의 소음을 낮춘다. aiohttp 접근 로그는 브릿지 진단에
    # 도움이 되지 않으면서 양이 많다.
    for noisy in ("aiohttp.access", "asyncio", "slack_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind(**kwargs: Any) -> None:
    """현재 태스크 컨텍스트에 필드를 묶는다.

    이벤트 처리 시작 시 bridge_id / event_key 를 묶어두면 이후 모든 로그에
    자동으로 붙는다. 태스크 로컬이므로 채널 워커 간에 섞이지 않는다.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear() -> None:
    structlog.contextvars.clear_contextvars()
