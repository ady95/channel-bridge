"""브릿지 운영 CLI. (FR-1.5, FR-9)

v1 의 관리 수단은 설정 파일 + CLI 다. 웹 UI 는 Phase 4.

    chbridge run        브릿지 기동
    chbridge migrate    마이그레이션만 적용
    chbridge status     브릿지·큐 상태 조회
    chbridge dlq        DLQ 조회 / 재처리
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from chbridge.config import Settings, load_env_files
from chbridge.obs import log as logsetup
from chbridge.store.db import Database, apply_migrations


async def _cmd_migrate() -> int:
    load_env_files()
    settings = Settings()
    logsetup.configure(level=settings.log_level)
    db = Database(settings.database_url)
    await db.connect()
    try:
        applied = await apply_migrations(db)
        for name in applied:
            sys.stdout.write(f"적용: {name}\n")
        if not applied:
            sys.stdout.write("이미 최신 상태입니다.\n")
    finally:
        await db.close()
    return 0


async def _cmd_status() -> int:
    from chbridge.app import build

    app = await build()
    try:
        out = sys.stdout
        out.write("\n[브릿지]\n")
        for bridge in app.router.bridges():
            state = "일시중지" if bridge.paused else "활성"
            out.write(f"  {bridge.id:<16} {state:<6} {bridge.name}\n")
            for ep in bridge.endpoints:
                out.write(f"      - {ep.platform.value:<11} {ep} ({ep.alias})\n")

        out.write("\n[큐 상태]\n")
        stats = await app.inbox.stats()
        if stats:
            for status, count in sorted(stats.items()):
                out.write(f"  {status:<12} {count}\n")
        else:
            out.write("  (비어 있음)\n")

        out.write("\n[백필 커서]\n")
        found = False
        for ep in app.router.all_endpoints():
            cursor = await app.cursors.get(ep.id)
            if cursor:
                found = True
                out.write(f"  {ep} -> {cursor}\n")
        if not found:
            out.write("  (없음 - 아직 이벤트를 받지 않았습니다)\n")
        out.write("\n")
    finally:
        for adapter in app.adapters.values():
            await adapter.close()
        await app.db.close()
    return 0


async def _cmd_dlq(*, requeue: bool, limit: int) -> int:
    load_env_files()
    settings = Settings()
    logsetup.configure(level=settings.log_level)
    db = Database(settings.database_url)
    await db.connect()
    try:
        rows = await db.fetch(
            """
            SELECT id, platform, channel_id, event_key, attempts, last_error, received_at
            FROM event_inbox WHERE status = 'dead'
            ORDER BY received_at DESC LIMIT $1
            """,
            limit,
        )
        if not rows:
            sys.stdout.write("DLQ 가 비어 있습니다.\n")
            return 0
        for r in rows:
            sys.stdout.write(
                f"#{r['id']} {r['platform']} {r['channel_id']} "
                f"attempts={r['attempts']}\n    {r['event_key']}\n"
                f"    {str(r['last_error'])[:200]}\n"
            )
        if requeue:
            n = await db.fetchval(
                """
                WITH revived AS (
                    UPDATE event_inbox SET status = 'pending', attempts = 0, last_error = NULL
                    WHERE status = 'dead' RETURNING 1
                )
                SELECT count(*) FROM revived
                """
            )
            sys.stdout.write(f"\n{int(n or 0)}건을 재처리 대기로 되돌렸습니다.\n")
    finally:
        await db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="chbridge", description="채널 브릿지")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="브릿지 기동")
    sub.add_parser("migrate", help="마이그레이션 적용")
    sub.add_parser("status", help="브릿지·큐 상태")
    dlq = sub.add_parser("dlq", help="DLQ 조회/재처리")
    dlq.add_argument("--requeue", action="store_true", help="DLQ 전체를 재처리 대기로")
    dlq.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    command = args.command or "run"

    if command == "run":
        from chbridge.app import run

        asyncio.run(run())
        return 0
    if command == "migrate":
        return asyncio.run(_cmd_migrate())
    if command == "status":
        return asyncio.run(_cmd_status())
    if command == "dlq":
        return asyncio.run(_cmd_dlq(requeue=args.requeue, limit=args.limit))

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
