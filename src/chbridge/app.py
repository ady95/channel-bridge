"""애플리케이션 조립과 기동.

기동 순서에 의미가 있다.
  1. 마이그레이션      — 이후 모든 것이 스키마에 의존한다
  2. 어댑터 open       — 자기 식별자를 얻어야 LoopGuard 가 성립한다
  3. Router 구축       — 채널 이름 해석 + bridges/endpoints 를 DB 에 반영
                          (message_links, sync_cursors 의 외래키 대상)
  4. Pipeline / Relay
  5. Supervisor 로 수신 루프 기동 — 각 루프는 백필 후 스트림에 붙는다
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass

import structlog

from chbridge.adapters.base import Adapter
from chbridge.adapters.mattermost import MattermostAdapter
from chbridge.adapters.slack import SlackAdapter
from chbridge.cir import Platform
from chbridge.config import AppConfig, Settings, load_env_files
from chbridge.loopguard import LoopGuard
from chbridge.obs import log as logsetup
from chbridge.relay import Relay
from chbridge.router import Router, build_router
from chbridge.store.cursors import CursorStore
from chbridge.store.db import Database, apply_migrations
from chbridge.store.inbox import InboxStore
from chbridge.store.links import MessageLinkStore
from chbridge.supervisor import JobFactory, Supervisor
from chbridge.worker import Pipeline

log = structlog.get_logger(__name__)


@dataclass
class App:
    settings: Settings
    config: AppConfig
    db: Database
    adapters: dict[str, Adapter]
    router: Router
    pipeline: Pipeline
    supervisor: Supervisor
    cursors: CursorStore
    inbox: InboxStore


def _make_adapter(workspace_id: str, config: AppConfig) -> Adapter:
    """워크스페이스 설정에 맞는 어댑터를 만든다. Platform 은 전수 처리된다."""
    ws = config.workspace(workspace_id)
    match ws.platform:
        case Platform.MATTERMOST:
            assert ws.base_url is not None  # config 검증에서 보장됨
            return MattermostAdapter(workspace_id=ws.id, base_url=ws.base_url, token=ws.token())
        case Platform.SLACK:
            app_token = ws.app_token()
            assert app_token is not None  # config 검증에서 보장됨
            return SlackAdapter(workspace_id=ws.id, bot_token=ws.token(), app_token=app_token)


async def build(settings: Settings | None = None) -> App:
    load_env_files()
    settings = settings or Settings()
    logsetup.configure(level=settings.log_level, json_output=settings.log_json)

    config = AppConfig.load(settings.bridges_file)
    log.info(
        "app.config_loaded",
        workspaces=[w.id for w in config.workspaces],
        bridges=[b.id for b in config.bridges],
    )

    db = Database(settings.database_url)
    await db.connect()

    # 브릿지에 실제로 참여하는 워크스페이스만 연결한다.
    needed = {ep.workspace for b in config.bridges for ep in b.endpoints}
    adapters: dict[str, Adapter] = {}
    try:
        await apply_migrations(db)
        for workspace_id in sorted(needed):
            adapter = _make_adapter(workspace_id, config)
            await adapter.open()
            adapters[workspace_id] = adapter

        router = await build_router(config, adapters, db)
    except BaseException:
        # 조립 도중 실패하면 이미 연 자원을 되감는다. 그러지 않으면 aiohttp
        # 세션과 DB 풀이 열린 채 남아, 원인 예외 뒤에 "Unclosed client
        # session" 경고만 잔뜩 붙어 진짜 원인을 가린다.
        await _release(adapters, db)
        raise

    links = MessageLinkStore(db)
    inbox = InboxStore(db)
    cursors = CursorStore(db)
    guard = LoopGuard(links, {ws: a.self_id() for ws, a in adapters.items()})
    relay = Relay(router=router, links=links, guard=guard, adapters=adapters)
    pipeline = Pipeline(
        inbox=inbox,
        relay=relay,
        router=router,
        cursors=cursors,
        max_attempts=settings.max_attempts,
    )

    supervisor = Supervisor()
    # 워커 관리자. 죽으면 전달이 멈추므로 반드시 재시작한다.
    supervisor.add("pipeline", pipeline.run)
    # 주기적 정합성 스윕. 재연결이 우리 모르게 일어나도 유실을 메운다.
    supervisor.add(
        "reconcile",
        _reconciler(adapters, router, cursors, pipeline, settings.reconcile_seconds),
    )
    for workspace_id, adapter in adapters.items():
        supervisor.add(
            f"listen:{workspace_id}",
            _listener(adapter, router, cursors, pipeline),
            min_backoff=1.0,
            max_backoff=30.0,
        )

    return App(
        settings=settings,
        config=config,
        db=db,
        adapters=adapters,
        router=router,
        pipeline=pipeline,
        supervisor=supervisor,
        cursors=cursors,
        inbox=inbox,
    )


async def _release(adapters: dict[str, Adapter], db: Database) -> None:
    """열린 자원을 모두 닫는다. 하나가 실패해도 나머지는 닫는다.

    정리 중 발생한 예외는 삼킨다. 기동 실패 경로에서는 원인 예외를,
    종료 경로에서는 종료 자체를 가려선 안 되기 때문이다.
    """
    for workspace_id, adapter in adapters.items():
        try:
            await adapter.close()
        except Exception as exc:
            log.warning("adapter.close_failed", workspace=workspace_id, error=str(exc)[:200])
    try:
        await db.close()
    except Exception as exc:
        log.warning("db.close_failed", error=str(exc)[:200])


def _listener(
    adapter: Adapter, router: Router, cursors: CursorStore, pipeline: Pipeline
) -> JobFactory:
    """수신 루프 팩토리.

    Supervisor 가 재시작할 때마다 **백필을 먼저 수행한 뒤** 스트림에 붙는다.
    이것이 FR-8(재연결 유실 복구)의 실제 동작 지점이다. WebSocket 은 끊긴
    동안의 이벤트를 재생해주지 않으므로 이 순서가 유일한 복구 수단이다.
    """

    async def run() -> None:
        for endpoint in router.channels_of(adapter.workspace_id):
            cursor = await cursors.get(endpoint.id)
            if cursor is None:
                # 첫 기동. 과거를 끌어오지 않고 현재 시점부터 시작한다.
                # (브릿지 생성 시 과거 대화 백필은 PRD 7 범위 외)
                continue
            for event in await adapter.backfill(endpoint.channel_id, cursor):
                await pipeline.submit(event)
        await adapter.listen(pipeline.submit)

    return run


def _reconciler(
    adapters: dict[str, Adapter],
    router: Router,
    cursors: CursorStore,
    pipeline: Pipeline,
    interval: float,
) -> JobFactory:
    """주기적 정합성 스윕. (NFR-2 보강)

    `listen()` 재시작 시의 백필만으로는 부족하다. `slack_sdk` 의
    SocketModeClient 는 **내부적으로 자동 재연결**하므로 우리 Supervisor 가
    재시작하지 않고, 그 구간의 이벤트가 조용히 유실된다. 로그에도 남지 않아
    발견이 매우 늦다.

    그래서 원인과 무관하게 커서 이후 구간을 주기적으로 다시 긁는다.
    정상 상태에서는 빈 결과가 돌아오므로 비용이 거의 없고, 중복은
    event_inbox 의 UNIQUE 제약이 차단한다.
    """

    async def run() -> None:
        while True:
            await asyncio.sleep(interval)
            for workspace_id, adapter in adapters.items():
                for endpoint in router.channels_of(workspace_id):
                    cursor = await cursors.get(endpoint.id)
                    if cursor is None:
                        continue
                    try:
                        events = await adapter.backfill(endpoint.channel_id, cursor)
                    except Exception as exc:  # 스윕 실패가 브릿지를 멈춰선 안 된다
                        log.warning(
                            "reconcile.failed",
                            endpoint=str(endpoint),
                            error=str(exc)[:200],
                        )
                        continue
                    for event in events:
                        await pipeline.submit(event)
                    if events:
                        log.info(
                            "reconcile.recovered",
                            endpoint=str(endpoint),
                            count=len(events),
                        )

    return run


async def run() -> None:
    app = await build()
    log.info(
        "app.starting",
        bridges=[b.id for b in app.router.bridges()],
        endpoints=[str(e) for e in app.router.all_endpoints()],
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        # Windows 에서는 add_signal_handler 가 없다. 개발 편의를 위해 무시한다.
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    runner = asyncio.create_task(app.supervisor.run(), name="supervisor")
    stopper = asyncio.create_task(stop.wait(), name="stop")
    try:
        done, _ = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if runner in done:
            # 수퍼바이저가 반환했다면 재시작 불가 잡이 죽은 것이다.
            runner.result()
    finally:
        log.info("app.stopping")
        for task in (runner, stopper):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
            await runner
        stats = await _safe_stats(app)
        await _release(app.adapters, app.db)
        log.info("app.stopped", inbox=stats)


async def _safe_stats(app: App) -> dict[str, int]:
    try:
        return await app.inbox.stats()
    except Exception:
        return {}
