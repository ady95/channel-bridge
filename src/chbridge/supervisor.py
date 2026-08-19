"""장기 태스크 감시. (리스크 R-3 대응)

Python asyncio 의 가장 흔한 함정에 대한 대응이다. `create_task` 로 띄운
태스크가 예외로 죽어도 아무 로그 없이 조용히 사라지고, 서비스는 "살아 있는데
전달이 안 되는" 상태가 된다. 브릿지에서 이건 침묵 장애이므로 가장 위험하다.

규칙: **모든 장기 태스크는 이 수퍼바이저를 경유한다.** 직접 create_task 하지 않는다.
  - 죽으면 백오프와 함께 재시작한다
  - 재시작 이력과 마지막 오류를 남긴다
  - 생존 상태를 health() 로 노출해 헬스체크·메트릭에서 볼 수 있게 한다
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

JobFactory = Callable[[], Awaitable[None]]


@dataclass
class JobState:
    name: str
    restart: bool
    starts: int = 0
    failures: int = 0
    running: bool = False
    last_error: str | None = None
    # 연속 실패 횟수. 성공적으로 오래 돌면 초기화된다.
    consecutive: int = 0


@dataclass
class _Job:
    name: str
    factory: JobFactory
    restart: bool
    min_backoff: float = 1.0
    max_backoff: float = 60.0
    state: JobState = field(init=False)

    def __post_init__(self) -> None:
        self.state = JobState(name=self.name, restart=self.restart)


class Supervisor:
    def __init__(self) -> None:
        self._jobs: list[_Job] = []

    def add(
        self,
        name: str,
        factory: JobFactory,
        *,
        restart: bool = True,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        self._jobs.append(
            _Job(
                name=name,
                factory=factory,
                restart=restart,
                min_backoff=min_backoff,
                max_backoff=max_backoff,
            )
        )

    def health(self) -> dict[str, JobState]:
        return {job.name: job.state for job in self._jobs}

    def all_running(self) -> bool:
        return all(job.state.running for job in self._jobs)

    async def run(self) -> None:
        """모든 잡을 띄우고 감시한다. 취소되면 전부 정리한다.

        TaskGroup 을 쓰므로 restart=False 인 잡이 죽으면 예외가 전파되고
        나머지도 함께 취소된다. 치명적 의존성(DB 등)에 이 설정을 쓴다.
        """
        async with asyncio.TaskGroup() as group:
            for job in self._jobs:
                group.create_task(self._supervise(job), name=f"job:{job.name}")

    async def _supervise(self, job: _Job) -> None:
        backoff = job.min_backoff
        while True:
            job.state.starts += 1
            job.state.running = True
            started = asyncio.get_running_loop().time()
            try:
                await job.factory()
                # 정상 반환. 무한 루프여야 할 잡이 반환한 것은 이상 신호다.
                job.state.last_error = None
                log.warning("supervisor.job_returned", job=job.name)
            except asyncio.CancelledError:
                job.state.running = False
                log.info("supervisor.job_cancelled", job=job.name)
                raise
            except Exception as exc:
                job.state.failures += 1
                job.state.last_error = f"{type(exc).__name__}: {exc}"
                log.error(
                    "supervisor.job_failed",
                    job=job.name,
                    error=str(exc)[:400],
                    failures=job.state.failures,
                    exc_info=True,
                )
            finally:
                job.state.running = False

            if not job.restart:
                log.error("supervisor.job_not_restarting", job=job.name)
                return

            # 충분히 오래 돌았다면 일시적 장애로 보고 백오프를 초기화한다.
            uptime = asyncio.get_running_loop().time() - started
            if uptime > 60:
                job.state.consecutive = 0
                backoff = job.min_backoff
            else:
                job.state.consecutive += 1
                backoff = min(backoff * 2, job.max_backoff)

            log.info("supervisor.job_restarting", job=job.name, delay=round(backoff, 1))
            await asyncio.sleep(backoff)
