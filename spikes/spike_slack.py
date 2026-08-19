"""T-0.4 / T-0.5 : Slack Socket Mode 이벤트 수신 + 표시 이름 오버라이드 실측

T-0.5 (오버라이드)
    chat.postMessage 에 username / icon_url 을 지정했을 때 실제로 적용되는지.
    `chat:write.customize` 스코프가 없으면 무시된다. 응답의 message 객체를
    확인해 프로그램으로 판정한다.

T-0.4 (이벤트 수신)
    Socket Mode 로 message / message_changed / message_deleted /
    reaction_added / reaction_removed 가 도달하는지.
    ★ 그리고 봇이 **자기 자신의 메시지** 이벤트를 받는지 — Mattermost 에서는
      받는 것이 확인됐다(LoopGuard 방어 ① 필수). Slack 도 같은지 확인한다.

    uv run python spikes/spike_slack.py            # 자동 검증 (봇 동작 기반)
    uv run python spikes/spike_slack.py --listen 60  # 수동 검증용 이벤트 덤프
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

DEV_DIR = Path(__file__).resolve().parent.parent / "dev"

FAKE_NAME = "Alice Kim (Mattermost)"
FAKE_ICON = "https://www.mattermost.org/wp-content/uploads/2016/04/icon.png"

WANTED = {"message", "reaction_added", "reaction_removed"}
WANTED_SUBTYPES = {"message_changed", "message_deleted"}


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for name in (".env", ".tokens.env"):
        path = DEV_DIR / name
        if not path.exists():
            sys.exit(f"필요한 파일이 없습니다: {path}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


class Collector:
    """Socket Mode 이벤트를 모은다."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def handle(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        # Slack 은 3초 내 ack 를 요구한다. 먼저 응답하고 처리한다.
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type == "events_api":
            event = (req.payload or {}).get("event") or {}
            self.events.append(event)


async def preflight(web: AsyncWebClient, channel: str) -> dict[str, Any]:
    print("=" * 78)
    print(" 사전 점검")
    print("=" * 78)

    auth = await web.auth_test()
    print(f"  워크스페이스 : {auth['team']} ({auth['team_id']})")
    print(f"  봇 사용자    : {auth['user']} (user_id={auth['user_id']})")
    print(f"  봇 id        : {auth.get('bot_id')}")

    info = await web.conversations_info(channel=channel)
    ch = info["channel"]
    print(f"  대상 채널    : #{ch['name']} ({ch['id']})")
    print(f"  봇 채널 참여 : {ch.get('is_member')}")
    if not ch.get("is_member"):
        sys.exit("  봇이 채널 멤버가 아닙니다. Slack 에서 /invite 해주세요.")
    return {"bot_id": auth.get("bot_id"), "bot_user_id": auth["user_id"]}


async def test_override(web: AsyncWebClient, channel: str) -> None:
    """T-0.5 : username / icon_url 오버라이드 적용 여부."""
    print()
    print("=" * 78)
    print(" T-0.5  표시 이름/아이콘 오버라이드")
    print("=" * 78)

    base = await web.chat_postMessage(channel=channel, text="[T-0.5] 기준선 (오버라이드 없음)")
    bmsg = base["message"]
    print(f"  기준선   username={bmsg.get('username')!r} subtype={bmsg.get('subtype')!r}")

    over = await web.chat_postMessage(
        channel=channel,
        text="[T-0.5] username + icon_url 오버라이드",
        username=FAKE_NAME,
        icon_url=FAKE_ICON,
    )
    omsg = over["message"]
    print(f"  오버라이드 username={omsg.get('username')!r} subtype={omsg.get('subtype')!r}")
    print(f"             icons={omsg.get('icons')}")

    print()
    if omsg.get("username") == FAKE_NAME:
        print("  ✓ username 오버라이드 적용됨. chat:write.customize 스코프 정상.")
        if omsg.get("icons"):
            print("  ✓ icon_url 도 적용됨.")
        else:
            print("  ! icons 필드가 비어 있다. 아이콘 적용은 육안 확인 필요.")
        print("    → Slack 측도 D-4 채택안(Bot 오버라이드)으로 충분하다.")
    else:
        print("  ✗ username 이 적용되지 않았다.")
        print("    → chat:write.customize 스코프 확인 필요. 없으면 앱 재설치.")


async def drive_events(web: AsyncWebClient, channel: str) -> dict[str, str]:
    """T-0.4 : 봇 동작으로 이벤트를 유발한다."""
    ids: dict[str, str] = {}

    root = await web.chat_postMessage(
        channel=channel,
        text="[T-0.4] 봇 원본 메시지",
        username=FAKE_NAME,
        icon_url=FAKE_ICON,
    )
    ids["root_ts"] = root["ts"]
    await asyncio.sleep(1.2)

    reply = await web.chat_postMessage(
        channel=channel,
        thread_ts=root["ts"],
        text="[T-0.4] 봇 쓰레드 답글",
        username=FAKE_NAME,
    )
    ids["reply_ts"] = reply["ts"]
    await asyncio.sleep(1.2)

    await web.chat_update(channel=channel, ts=root["ts"], text="[T-0.4] 봇 원본 메시지 (편집됨)")
    await asyncio.sleep(1.2)

    await web.reactions_add(channel=channel, timestamp=root["ts"], name="thumbsup")
    await asyncio.sleep(1.2)
    await web.reactions_remove(channel=channel, timestamp=root["ts"], name="thumbsup")
    await asyncio.sleep(1.2)

    await web.chat_delete(channel=channel, ts=reply["ts"])
    await asyncio.sleep(1.2)

    return ids


def report(events: list[dict[str, Any]], ids: dict[str, str], who: dict[str, Any]) -> None:
    print()
    print("=" * 78)
    print(" T-0.4  수신 이벤트")
    print("=" * 78)
    kinds = Counter(
        f"{e.get('type')}" + (f" / {e['subtype']}" if e.get("subtype") else "") for e in events
    )
    for kind, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"    {kind:<40} {n}건")

    got_types = {e.get("type") for e in events}
    got_subs = {e.get("subtype") for e in events if e.get("subtype")}

    print()
    print("  필수 이벤트 도달 여부")
    for t in sorted(WANTED):
        print(f"    {'✓' if t in got_types else '✗'} {t}")
    for st in sorted(WANTED_SUBTYPES):
        print(f"    {'✓' if st in got_subs else '✗'} message / {st}")

    # ------------------------------------------------ 자기 발신 여부
    print()
    print("=" * 78)
    print(" ★ 자기 발신 이벤트 수신 여부 (LoopGuard 설계 근거)")
    print("=" * 78)
    bot_id = who.get("bot_id")
    bot_user = who.get("bot_user_id")
    self_msgs = [
        e
        for e in events
        if e.get("type") == "message"
        and not e.get("subtype")
        and (e.get("bot_id") == bot_id or e.get("user") == bot_user)
    ]
    if self_msgs:
        s = self_msgs[0]
        print(f"  ✓ 봇이 자기 메시지 이벤트를 받는다 ({len(self_msgs)}건)")
        print("     → Mattermost 와 동일. LoopGuard 방어 ①이 양쪽 모두 필수.")
        print("     판별 필드:")
        for k in ("bot_id", "user", "app_id", "username", "subtype", "thread_ts"):
            print(f"       {k:<10} = {s.get(k)!r}")
    else:
        print("  ✗ 자기 메시지 이벤트가 오지 않았다.")
        print("     → Slack 은 자기 발신을 흘려주지 않는다. 매핑 조회(②)가 주 방어선.")

    # ------------------------------------------------ 쓰레드
    # ------------------------------------------------ 식별 필드 전수 덤프
    # LoopGuard 는 무관용 지표다. 오버라이드한 메시지(subtype=bot_message)가
    # 우리 봇/앱 식별자를 담고 있는지 전수로 확인해야 필터를 확정할 수 있다.
    print()
    print("=" * 78)
    print(" 모든 message 이벤트의 식별 필드 (LoopGuard 필터 확정용)")
    print("=" * 78)
    print(f"  우리 bot_id={bot_id!r}  bot_user_id={bot_user!r}")
    print()
    print(f"  {'subtype':<18}{'bot_id':<16}{'app_id':<14}{'user':<14}{'username':<24}")
    print("  " + "-" * 84)

    def identity_of(ev: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """식별 필드가 실린 위치를 찾는다.

        message_changed  -> ev["message"]
        message_deleted  -> ev["previous_message"]   (message 키가 없다)
        그 외            -> ev 자체
        """
        for key in ("message", "previous_message"):
            node = ev.get(key)
            if isinstance(node, dict):
                return node, key
        return ev, "(최상위)"

    undetectable: list[str] = []
    for e in events:
        if e.get("type") != "message":
            continue
        src, where = identity_of(e)
        has_bot = src.get("bot_id") == bot_id
        print(
            f"  {e.get('subtype') or '(없음)'!s:<18}"
            f"{src.get('bot_id') or '—'!s:<16}"
            f"{src.get('app_id') or '—'!s:<14}"
            f"{src.get('user') or '—'!s:<14}"
            f"{src.get('username') or '—'!s:<24}"
            f"{where:<18}{'✓' if has_bot else '✗'}"
        )
        if not has_bot:
            undetectable.append(str(e.get("subtype") or "(없음)"))

    print()
    if not undetectable:
        print("  ✓ 모든 message 이벤트에서 bot_id 로 자기 발신을 판별할 수 있다.")
        print("    → Slack LoopGuard 1차 필터: bot_id == 우리 bot_id")
        print("      단, 식별 필드 위치가 subtype 마다 다르다:")
        print("        message_changed -> message.bot_id")
        print("        message_deleted -> previous_message.bot_id")
        print("        그 외           -> 최상위 bot_id")
    else:
        print(f"  ✗ bot_id 로 판별 불가한 이벤트: {sorted(set(undetectable))}")
        print("    → 해당 이벤트는 방어 ①로 걸러낼 수 없다.")
        print("      MessageLink 조회(방어 ②)가 유일한 방어선이 된다. 필수 구현.")

    print()
    print("=" * 78)
    print(" 쓰레드 식별 (thread_ts)")
    print("=" * 78)
    found = False
    for e in events:
        if e.get("ts") == ids.get("reply_ts"):
            print(f"  ✓ 답글 이벤트 thread_ts = {e.get('thread_ts')!r}")
            print(f"     기대값(원본 ts)     = {ids.get('root_ts')!r}")
            found = True
    if not found:
        print("  ! 답글 이벤트를 특정하지 못했다 (아래 원본 덤프 참조)")

    # ------------------------------------------------ 편집/삭제 구조
    print()
    print("=" * 78)
    print(" 편집 / 삭제 이벤트 구조")
    print("=" * 78)
    for st in ("message_changed", "message_deleted"):
        ev = next((e for e in events if e.get("subtype") == st), None)
        if ev is None:
            print(f"  {st}: 수신 없음")
            continue
        print(f"  {st}:")
        print(f"    최상위 키 : {sorted(ev.keys())}")
        if st == "message_changed":
            inner = ev.get("message") or {}
            prev = ev.get("previous_message") or {}
            print(f"    message.ts        = {inner.get('ts')!r}")
            print(f"    message.text      = {inner.get('text')!r}")
            print(f"    previous.text     = {prev.get('text')!r}")
        else:
            print(f"    deleted_ts        = {ev.get('deleted_ts')!r}")

    print()
    print("=" * 78)
    print(" 판정")
    print("=" * 78)
    missing = (WANTED - got_types) | (WANTED_SUBTYPES - got_subs)
    if not missing:
        print("  ✓ 브릿지에 필요한 이벤트 전부 수신. T-0.4 통과.")
    else:
        print(f"  ✗ 누락: {sorted(missing)}")
        print("     사람이 보낸 메시지는 봇 동작으로 재현되지 않을 수 있다.")
        print("     --listen 모드로 직접 입력해 확인하세요.")


async def main() -> int:
    env = load_env()
    app_token = env.get("SLACK_APP_TOKEN")
    bot_token = env.get("SLACK_BOT_TOKEN")
    channel = env.get("SLACK_TEST_CHANNEL")
    if not (app_token and bot_token and channel):
        sys.exit("SLACK_APP_TOKEN / SLACK_BOT_TOKEN / SLACK_TEST_CHANNEL 이 필요합니다.")

    listen_only = "--listen" in sys.argv
    seconds = 60.0
    if listen_only:
        idx = sys.argv.index("--listen")
        if idx + 1 < len(sys.argv):
            seconds = float(sys.argv[idx + 1])

    web = AsyncWebClient(token=bot_token)
    who = await preflight(web, channel)

    collector = Collector()
    sm = SocketModeClient(app_token=app_token, web_client=web)
    sm.socket_mode_request_listeners.append(collector.handle)  # type: ignore[arg-type]

    await sm.connect()
    print("\n  Socket Mode 연결 확립.\n")

    try:
        if listen_only:
            print("=" * 78)
            print(f" 수동 검증 모드 — {seconds:.0f}초간 이벤트를 수집합니다")
            print("=" * 78)
            print("  Slack 채널에서 직접 해보세요:")
            print("    1) 메시지 입력")
            print("    2) 그 메시지에 이모지 리액션 추가 후 제거")
            print("    3) 메시지 편집")
            print("    4) 쓰레드 답글")
            print("    5) 메시지 삭제")
            print()
            for remain in range(int(seconds), 0, -10):
                print(f"    ... {remain}초 남음 (수집 {len(collector.events)}건)")
                await asyncio.sleep(min(10, remain))
            report(collector.events, {}, who)
            print()
            print("=" * 78)
            print(" 원본 이벤트 덤프")
            print("=" * 78)
            for i, e in enumerate(collector.events, 1):
                sub = f"/{e['subtype']}" if e.get("subtype") else ""
                print(f"  [{i}] {e.get('type')}{sub}  keys={sorted(e.keys())}")
        else:
            await test_override(web, channel)
            ids = await drive_events(web, channel)
            await asyncio.sleep(2.5)
            report(collector.events, ids, who)
    finally:
        await sm.disconnect()
        await sm.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
