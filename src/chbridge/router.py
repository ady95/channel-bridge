"""브릿지 라우팅. (FR-1, PRD 5.1)

Router 는 대상 Endpoint 의 플랫폼 종류를 알지 못한다. 그 덕분에 MM↔MM 이
별도 코드 없이 성립한다.

기동 시 채널 이름을 id 로 해석하고 bridges/endpoints 를 DB 에 반영한다.
sync_cursors 와 message_links 가 이 행들을 참조하므로 반드시 선행돼야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from chbridge.adapters.base import Adapter
from chbridge.cir import MessageRef, Platform
from chbridge.config import AppConfig, BridgeOptions
from chbridge.store.db import Database

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Endpoint:
    id: str
    bridge_id: str
    platform: Platform
    workspace_id: str
    channel_id: str
    alias: str

    def ref(self, message_id: str = "") -> MessageRef:
        return MessageRef(
            platform=self.platform,
            workspace_id=self.workspace_id,
            channel_id=self.channel_id,
            message_id=message_id,
        )

    def __str__(self) -> str:
        return f"{self.workspace_id}/{self.channel_id}"


@dataclass(frozen=True, slots=True)
class Bridge:
    id: str
    name: str
    options: BridgeOptions
    endpoints: tuple[Endpoint, ...]
    paused: bool


class Router:
    def __init__(self, bridges: list[Bridge]) -> None:
        self._bridges = {b.id: b for b in bridges}
        self._by_channel: dict[tuple[str, str, str], Endpoint] = {}
        for bridge in bridges:
            for ep in bridge.endpoints:
                self._by_channel[(ep.platform.value, ep.workspace_id, ep.channel_id)] = ep

    def endpoint_for(self, ref: MessageRef) -> Endpoint | None:
        return self._by_channel.get((ref.platform.value, ref.workspace_id, ref.channel_id))

    def bridge_of(self, bridge_id: str) -> Bridge:
        return self._bridges[bridge_id]

    def targets(self, source: Endpoint) -> list[Endpoint]:
        """반대편 Endpoint 목록. 자기 자신은 제외한다."""
        bridge = self._bridges[source.bridge_id]
        return [ep for ep in bridge.endpoints if ep.id != source.id]

    def all_endpoints(self) -> list[Endpoint]:
        return [ep for b in self._bridges.values() for ep in b.endpoints]

    def channels_of(self, workspace_id: str) -> list[Endpoint]:
        return [ep for ep in self.all_endpoints() if ep.workspace_id == workspace_id]

    def bridges(self) -> list[Bridge]:
        return list(self._bridges.values())


async def build_router(config: AppConfig, adapters: dict[str, Adapter], db: Database) -> Router:
    """채널 이름을 해석하고 DB 에 반영한 뒤 Router 를 만든다."""
    bridges: list[Bridge] = []

    for bconf in config.bridges:
        endpoints: list[Endpoint] = []
        for econf in bconf.endpoints:
            adapter = adapters.get(econf.workspace)
            if adapter is None:
                log.warning(
                    "router.workspace_not_active",
                    bridge=bconf.id,
                    workspace=econf.workspace,
                    detail="어댑터가 활성화되지 않아 이 브릿지를 건너뜁니다",
                )
                endpoints = []
                break

            if econf.channel_id:
                channel_id = econf.channel_id
            else:
                assert econf.channel is not None  # config 검증에서 보장됨
                channel_id = await adapter.resolve_channel(team=econf.team, name=econf.channel)

            ws = config.workspace(econf.workspace)
            endpoints.append(
                Endpoint(
                    id=f"{bconf.id}:{econf.workspace}:{channel_id}",
                    bridge_id=bconf.id,
                    platform=adapter.platform,
                    workspace_id=econf.workspace,
                    channel_id=channel_id,
                    alias=ws.alias,
                )
            )

        if len(endpoints) < 2:
            log.warning("router.bridge_skipped", bridge=bconf.id, resolved=len(endpoints))
            continue

        bridges.append(
            Bridge(
                id=bconf.id,
                name=bconf.name,
                options=bconf.options,
                endpoints=tuple(endpoints),
                paused=bconf.paused,
            )
        )
        log.info(
            "router.bridge_ready",
            bridge=bconf.id,
            name=bconf.name,
            endpoints=[str(e) for e in endpoints],
        )

    await _persist(bridges, db)
    return Router(bridges)


async def _persist(bridges: list[Bridge], db: Database) -> None:
    """bridges / endpoints 를 DB 에 반영한다.

    message_links.bridge_id 와 sync_cursors.endpoint_id 가 외래키로 참조하므로
    선행 필수다. endpoints 의 UNIQUE 제약이 토폴로지 사이클을 다시 한 번
    막아준다 — 설정 검증과 DB 제약의 이중 방어.
    """
    async with db.transaction() as conn:
        for bridge in bridges:
            await conn.execute(
                """
                INSERT INTO bridges (id, name, status, options)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (id) DO UPDATE
                    SET name = EXCLUDED.name,
                        status = EXCLUDED.status,
                        options = EXCLUDED.options,
                        updated_at = now()
                """,
                bridge.id,
                bridge.name,
                "paused" if bridge.paused else "active",
                bridge.options.model_dump_json(),
            )
            # 설정에서 사라진 Endpoint 는 정리한다. 남겨두면 오래된 채널로
            # 전달을 시도한다.
            await conn.execute(
                "DELETE FROM endpoints WHERE bridge_id = $1 AND id <> ALL($2::text[])",
                bridge.id,
                [ep.id for ep in bridge.endpoints],
            )
            for ep in bridge.endpoints:
                await conn.execute(
                    """
                    INSERT INTO endpoints
                        (id, bridge_id, platform, workspace_id, channel_id, alias)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (id) DO UPDATE
                        SET channel_id = EXCLUDED.channel_id, alias = EXCLUDED.alias
                    """,
                    ep.id,
                    ep.bridge_id,
                    ep.platform.value,
                    ep.workspace_id,
                    ep.channel_id,
                    ep.alias,
                )
