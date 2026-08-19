"""채널 단위 FIFO 워커. (PRD 5.9 / FR-8)

순서 보장 방식: **채널당 워커 하나.** 그래서 같은 채널의 이벤트는 절대
추월되지 않고, 서로 다른 브릿지는 완전히 병렬로 처리된다.

이벤트는 먼저 event_inbox 에 기록된 뒤 워커가 집어간다. 이 순서가
"재시작 후 유실 0건"(NFR-2)의 근거다.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from chbridge.adapters.base import AdapterError
from chbridge.cir import Event
from chbridge.obs import log as logsetup
from chbridge.relay import Relay
from chbridge.router import Router
from chbridge.store.cursors import CursorStore
from chbridge.store.inbox import InboxStore

log = structlog.get_logger(__name__)

# 워커가 할 일이 없을 때 깨어나 확인하는 주기.
# 신규 이벤트는 submit() 이 즉시 깨우므로 이 값은 안전망일 뿐이다.
_IDLE_POLL_SECONDS = 5.0


class Pipeline:
    """이벤트 수납 → 채널별 워커 처리.

    어댑터는 submit() 만 호출한다. 큐 영속화와 순서 보장은 여기서 처리한다.
    """

    def __init__(
        self,
        *,
        inbox: InboxStore,
        relay: Relay,
        router: Router,
        cursors: CursorStore,
        max_attempts: int,
    ) -> None:
        self._inbox = inbox
        self._relay = relay
        self._router = router
        self._cursors = cursors
        self._max_attempts = max_attempts
        self._wakes: dict[str, asyncio.Event] = {}
        self._group: asyncio.TaskGroup | None = None
        self._running: set[str] = set()

    # ------------------------------------------------------------ 수납

    async def submit(self, event: Event) -> None:
        """어댑터가 호출하는 진입점.

        브릿지에 속하지 않은 채널의 이벤트는 큐에 넣지도 않는다.
        (워크스페이스 전체 이벤트가 흘러 들어오므로 여기서 걸러야 한다)
        """
        endpoint = self._router.endpoint_for(event.source)
        if endpoint is None:
            return

        accepted = await self._inbox.enqueue(event, bridge_id=endpoint.bridge_id)

        # ★ 커서는 **큐 적재 성공 직후** 전진시킨다. 처리 완료를 기다리지 않는다.
        #   inbox 가 영속이므로 적재된 이벤트는 더 이상 유실될 수 없고,
        #   따라서 커서를 넘겨도 안전하다. 처리 완료 시점까지 미루면 처리가
        #   막힌 동안 재연결 백필이 같은 구간을 계속 되짚는다.
        if event.cursor:
            await self._cursors.advance(endpoint.id, event.cursor)

        if not accepted:
            return

        self._ensure_worker(event.source.channel_id)
        self._wakes[event.source.channel_id].set()

    # ------------------------------------------------------------ 실행

    async def run(self) -> None:
        """워커들을 관리한다. 취소되면 전부 정리된다."""
        # 직전 크래시로 'processing' 에 남은 행을 회수한다.
        # 이것을 빠뜨리면 중단 시점의 이벤트가 영구히 멈춘다 - NFR-2 위반.
        await self._inbox.requeue_all_processing()

        async with asyncio.TaskGroup() as group:
            self._group = group
            # 미처리 이벤트가 있는 채널의 워커를 먼저 띄운다.
            for channel_id in await self._inbox.pending_channels():
                self._ensure_worker(channel_id)
            # 브릿지에 속한 모든 채널의 워커를 미리 띄워둔다.
            # 워커는 유휴 시 비용이 없고, 이러면 첫 메시지 지연이 사라진다.
            for endpoint in self._router.all_endpoints():
                self._ensure_worker(endpoint.channel_id)
            # TaskGroup 은 자식이 모두 끝나면 반환한다. 워커는 무한 루프이므로
            # 취소될 때까지 여기 머문다.

    def _ensure_worker(self, channel_id: str) -> None:
        if channel_id not in self._wakes:
            self._wakes[channel_id] = asyncio.Event()
        if channel_id in self._running or self._group is None:
            return
        self._running.add(channel_id)
        self._group.create_task(self._worker(channel_id), name=f"worker:{channel_id[:10]}")

    async def _worker(self, channel_id: str) -> None:
        wake = self._wakes[channel_id]
        log.debug("worker.started", channel_id=channel_id)
        while True:
            try:
                item = await self._inbox.claim(channel_id)
            except Exception as exc:
                log.error("worker.claim_failed", channel_id=channel_id, error=str(exc))
                await asyncio.sleep(_IDLE_POLL_SECONDS)
                continue

            if item is None:
                wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=_IDLE_POLL_SECONDS)
                continue

            await self._process(item.row_id, item.event, item.attempts)

    async def _process(self, row_id: int, event: Event, attempts: int) -> None:
        logsetup.bind(
            event_key=event.event_key,
            kind=event.kind.value,
            channel_id=event.source.channel_id,
        )
        try:
            await self._relay.handle(event)
            await self._inbox.mark_done(row_id)
        except AdapterError as exc:
            await self._fail(row_id, event, exc, retryable=exc.retryable, attempts=attempts)
        except Exception as exc:
            await self._fail(row_id, event, exc, retryable=True, attempts=attempts)
        finally:
            logsetup.clear()

    async def _fail(
        self,
        row_id: int,
        event: Event,
        exc: BaseException,
        *,
        retryable: bool,
        attempts: int,
    ) -> None:
        # 재시도 불가한 오류는 즉시 DLQ 로 보낸다. 같은 요청을 반복해도
        # 결과가 같은데 재시도하면 큐가 막힌다.
        limit = self._max_attempts if retryable else 0
        dead = await self._inbox.mark_failed(
            row_id, f"{type(exc).__name__}: {exc}", max_attempts=limit
        )
        log.warning(
            "worker.event_failed",
            row_id=row_id,
            attempts=attempts,
            retryable=retryable,
            dead=dead,
            error=str(exc)[:300],
        )
        if not dead:
            # 지수 백오프. 부모가 아직 매핑되지 않은 답글 등은 곧 풀린다.
            await asyncio.sleep(min(2.0 * attempts, 30.0))
            self._wakes[event.source.channel_id].set()
