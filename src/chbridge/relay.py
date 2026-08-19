"""한 방향 전달 실행. (PRD 2 용어정의 - Relay)

Endpoint 가 2개인 Bridge = Relay 2개. 이 클래스는 "CIR 이벤트 하나를 반대편
모든 Endpoint 에 반영한다"는 한 가지 일만 한다.

쓰레드 부모 변환(PRD 5.7)이 여기서 일어난다. 원본 플랫폼의 부모 id 를
MessageLink 로 조회해 대상 플랫폼의 부모 id 로 바꿔 넣는다.
"""

from __future__ import annotations

import uuid

import structlog

from chbridge.adapters.base import Adapter, AdapterError
from chbridge.cir import Event, EventKind, FileAttachment, OutboundMessage
from chbridge.loopguard import LoopGuard
from chbridge.router import Endpoint, Router
from chbridge.store.links import MessageLinkStore
from chbridge.transform.files import SkipReason, append_notice, skip_notice

log = structlog.get_logger(__name__)


class ParentNotMappedError(AdapterError):
    """쓰레드 부모가 아직 반대편에 매핑되지 않았다.

    답글이 부모보다 먼저 처리되면 발생한다. 재시도로 해소되는 경우가 많으므로
    retryable 로 둔다. 재시도 한계를 넘으면 DLQ 로 가고, 그때는 채널 최상위로
    격하하는 정책을 적용한다. (PRD 5.7)
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class Relay:
    def __init__(
        self,
        *,
        router: Router,
        links: MessageLinkStore,
        guard: LoopGuard,
        adapters: dict[str, Adapter],
    ) -> None:
        self._router = router
        self._links = links
        self._guard = guard
        self._adapters = adapters

    async def handle(self, event: Event) -> None:
        source = self._router.endpoint_for(event.source)
        if source is None:
            log.debug("relay.no_endpoint", ref=str(event.source))
            return

        bridge = self._router.bridge_of(source.bridge_id)
        if bridge.paused:
            log.info("relay.bridge_paused", bridge=bridge.id)
            return

        drop = await self._guard.evaluate(
            event, relay_bot_messages=bridge.options.relay_bot_messages
        )
        if drop is not None:
            log.debug("relay.dropped", reason=drop.value, ref=str(event.source))
            return

        targets = self._router.targets(source)
        if not targets:
            return

        match event.kind:
            case EventKind.MESSAGE_CREATED:
                await self._on_created(event, source, targets)
            case EventKind.MESSAGE_EDITED:
                if bridge.options.relay_edits:
                    await self._on_edited(event, source, targets)
            case EventKind.MESSAGE_DELETED:
                if bridge.options.relay_deletes:
                    await self._on_deleted(event, source, targets)
            case EventKind.REACTION_ADDED | EventKind.REACTION_REMOVED:
                # Phase 3. 지금은 조용히 무시하지 않고 흔적을 남긴다.
                log.debug("relay.reaction_not_implemented", kind=event.kind.value)

    # ------------------------------------------------------------ 생성

    async def _on_created(self, event: Event, source: Endpoint, targets: list[Endpoint]) -> None:
        if not event.text and not event.files:
            return

        group_id = await self._links.record_origin(
            bridge_id=source.bridge_id,
            ref=event.source,
            author_ref=event.author.platform_user_id if event.author else None,
        )

        bridge = self._router.bridge_of(source.bridge_id)
        for target in targets:
            parent_id = await self._resolve_parent(event, source, target)
            adapter = self._adapters[target.workspace_id]
            file_ids, notice = await self._transfer_files(
                event, source, target, relay_files=bridge.options.relay_files
            )
            outbound = OutboundMessage(
                text=append_notice(event.text, notice),
                author=event.author,
                parent_message_id=parent_id,
                file_ids=file_ids,
                source_alias=source.alias,
            )
            new_id = await adapter.send(target.channel_id, outbound)

            # ★ 게시 직후 즉시 기록해야 한다. 자기 발신 이벤트가 곧 도착하며,
            #   방어 ②가 성립하려면 그 전에 Replica 로 등록돼 있어야 한다.
            await self._links.record_replica(
                bridge_id=source.bridge_id,
                group_id=group_id,
                ref=target.ref(new_id),
                author_ref=event.author.platform_user_id if event.author else None,
            )
            log.info(
                "relay.delivered",
                group_id=str(group_id),
                source=str(source),
                target=str(target),
                message_id=new_id,
                threaded=parent_id is not None,
            )

    # ------------------------------------------------------------ 첨부 전달

    async def _transfer_files(
        self, event: Event, source: Endpoint, target: Endpoint, *, relay_files: bool
    ) -> tuple[tuple[str, ...], str]:
        """첨부를 원본에서 대상으로 흘려보낸다. (업로드된 id, 안내 문구) 반환.

        바이트는 브릿지를 통과만 하고 어디에도 저장되지 않는다. 그래서 파일
        크기가 브릿지의 자원 요구를 늘리지 않는다.

        전달하지 못한 첨부는 예외로 터뜨리지 않고 안내 문구로 만든다. 첨부
        하나 때문에 본문까지 통째로 재시도 큐에 들어가면, 영영 전달되지 않는
        메시지가 생긴다. 본문은 반드시 건너가야 한다.
        """
        if not event.files:
            return (), ""

        src = self._adapters[source.workspace_id]
        dst = self._adapters[target.workspace_id]
        limit = dst.max_file_size()

        uploaded: list[str] = []
        skipped: list[tuple[FileAttachment, SkipReason]] = []

        for file in event.files:
            reason = self._precheck(file, relay_files=relay_files, dst=dst, limit=limit)
            if reason is not None:
                skipped.append((file, reason))
                continue
            try:
                async with src.open_file(file) as chunks:
                    uploaded.append(await dst.upload_file(target.channel_id, file, chunks))
            except AdapterError as exc:
                log.warning(
                    "relay.file_failed",
                    target=str(target),
                    file=file.name,
                    size=file.size,
                    error=str(exc)[:200],
                )
                skipped.append((file, SkipReason.FAILED))

        if skipped:
            log.info(
                "relay.files_skipped",
                target=str(target),
                delivered=len(uploaded),
                skipped=[(f.name, r.value) for f, r in skipped],
            )
        return tuple(uploaded), skip_notice(skipped, limit=limit)

    @staticmethod
    def _precheck(
        file: FileAttachment, *, relay_files: bool, dst: Adapter, limit: int
    ) -> SkipReason | None:
        """올려보기 전에 알 수 있는 실패를 걸러낸다.

        크기 초과를 미리 잡는 이유는 대역폭이다. 300MB 를 다 보내고 나서
        413 을 받는 것은 낭비다. 다만 limit 이 0(미상)이면 검사하지 않고
        업로드 결과로 판정한다.
        """
        if not relay_files:
            return SkipReason.RELAY_DISABLED
        if not dst.supports_upload():
            return SkipReason.UNSUPPORTED
        if limit and file.size > limit:
            return SkipReason.TOO_LARGE
        return None

    async def _resolve_parent(self, event: Event, source: Endpoint, target: Endpoint) -> str | None:
        """원본 쪽 부모 id 를 대상 쪽 부모 id 로 변환한다. (PRD 5.7)"""
        if event.parent_message_id is None:
            return None

        parent_ref = source.ref(event.parent_message_id)
        group_id = await self._links.find_group_for(parent_ref)
        if group_id is None:
            raise ParentNotMappedError(f"쓰레드 부모가 아직 매핑되지 않았습니다: {parent_ref}")

        mapped = await self._links.replica_in(group_id, endpoint_ref=target.ref())
        if mapped is None:
            raise ParentNotMappedError(
                f"부모가 대상 채널에 아직 없습니다: group={group_id} target={target}"
            )
        return mapped

    # ------------------------------------------------------------ 편집

    async def _on_edited(self, event: Event, source: Endpoint, targets: list[Endpoint]) -> None:
        group_id = await self._links.find_group_for(event.source)
        if group_id is None:
            log.info("relay.edit_unmapped", ref=str(event.source))
            return

        for target in targets:
            message_id = await self._links.replica_in(group_id, endpoint_ref=target.ref())
            if message_id is None:
                continue
            adapter = self._adapters[target.workspace_id]
            await adapter.edit(
                target.channel_id,
                message_id,
                OutboundMessage(text=event.text, author=event.author, source_alias=source.alias),
            )
            log.info(
                "relay.edited",
                group_id=str(group_id),
                target=str(target),
                message_id=message_id,
            )

    # ------------------------------------------------------------ 삭제

    async def _on_deleted(self, event: Event, source: Endpoint, targets: list[Endpoint]) -> None:
        group_id = await self._links.find_group_for(event.source)
        if group_id is None:
            log.info("relay.delete_unmapped", ref=str(event.source))
            return

        for target in targets:
            message_id = await self._links.replica_in(group_id, endpoint_ref=target.ref())
            if message_id is None:
                continue
            adapter = self._adapters[target.workspace_id]
            await adapter.delete(target.channel_id, message_id)
            log.info(
                "relay.deleted",
                group_id=str(group_id),
                source=str(source),
                target=str(target),
            )

        # 그룹을 정리한다. 남겨두면 같은 id 가 재사용될 때 오작동한다.
        await self._links.delete_group(group_id)

    # ------------------------------------------------------------ 진단

    async def group_of(self, event: Event) -> uuid.UUID | None:
        return await self._links.find_group_for(event.source)
