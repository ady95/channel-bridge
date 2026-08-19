"""T-0.3 : Mattermost WebSocket 이벤트 수신 실측

확인할 것
  1. 필요한 이벤트가 모두 오는가
     posted / post_edited / post_deleted / reaction_added / reaction_removed
  2. 각 이벤트에서 브릿지가 필요한 필드를 얻을 수 있는가
     post id, channel_id, user_id, root_id, props
  3. ★ 봇이 **자기 자신의 게시물**에 대한 이벤트를 받는가
     받는다면 LoopGuard 방어 ①(자기 발신 무시)이 필수가 된다. (PRD 5.1)
  4. mattermostautodriver 의 websocket 진입점이 우리 감시 모델에 쓸 수 있는 형태인가

WebSocket 은 raw aiohttp 로 직접 구현한다. 프로토콜이 단순하고, 프로덕션에서
태스크 수퍼바이저와 결합하려면 연결 수명주기를 직접 통제해야 하기 때문이다.

    uv run python spikes/spike_ws.py
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import aiohttp

DEV_DIR = Path(__file__).resolve().parent.parent / "dev"
TEAM = "bridge"
CHANNEL = "bridge-1"
ACTOR_LOGIN = "alice@test.local"
PASSWORD = "Bridge-Test-1234"

WANTED = {"posted", "post_edited", "post_deleted", "reaction_added", "reaction_removed"}
COLLECT_SECONDS = 12.0


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


async def listen(
    ws_url: str, token: str, events: list[dict[str, Any]], ready: asyncio.Event
) -> None:
    """MM WebSocket 에 붙어 이벤트를 수집한다."""
    async with aiohttp.ClientSession() as s, s.ws_connect(ws_url, heartbeat=30) as ws:
        # MM 은 접속 후 authentication_challenge 를 보내야 이벤트를 흘려준다.
        await ws.send_json(
            {"seq": 1, "action": "authentication_challenge", "data": {"token": token}}
        )
        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            payload = json.loads(msg.data)
            if payload.get("event") == "hello":
                ready.set()
            events.append(payload)


async def trigger(base: str, env: dict[str, str], channel_id: str) -> dict[str, str]:
    """이벤트를 유발한다. alice(일반 사용자)와 bridgebot(봇) 양쪽으로."""
    ids: dict[str, str] = {}
    bot_token = env["MM_A_BOT_TOKEN"]

    async with aiohttp.ClientSession() as s:
        # alice 세션
        async with s.post(
            f"{base}/users/login", json={"login_id": ACTOR_LOGIN, "password": PASSWORD}
        ) as r:
            if r.status != 200:
                sys.exit(f"alice 로그인 실패 HTTP {r.status}")
            alice_h = {"Authorization": f"Bearer {r.headers.get('Token', '')}"}
            alice_id = (await r.json())["id"]
        ids["alice_id"] = alice_id

        # 1) alice 게시
        async with s.post(
            f"{base}/posts",
            headers=alice_h,
            json={"channel_id": channel_id, "message": "[T-0.3] alice 원본 메시지"},
        ) as r:
            root = await r.json()
        ids["alice_post"] = root["id"]
        await asyncio.sleep(0.6)

        # 2) alice 쓰레드 답글 (root_id 전달 확인용)
        async with s.post(
            f"{base}/posts",
            headers=alice_h,
            json={
                "channel_id": channel_id,
                "message": "[T-0.3] alice 쓰레드 답글",
                "root_id": root["id"],
            },
        ) as r:
            reply = await r.json()
        ids["alice_reply"] = reply["id"]
        await asyncio.sleep(0.6)

        # 3) alice 편집
        async with s.put(
            f"{base}/posts/{root['id']}/patch",
            headers=alice_h,
            json={"message": "[T-0.3] alice 원본 메시지 (편집됨)"},
        ) as r:
            await r.read()
        await asyncio.sleep(0.6)

        # 4) alice 리액션 추가/제거
        reaction = {"user_id": alice_id, "post_id": root["id"], "emoji_name": "thumbsup"}
        async with s.post(f"{base}/reactions", headers=alice_h, json=reaction) as r:
            await r.read()
        await asyncio.sleep(0.6)
        async with s.delete(
            f"{base}/users/{alice_id}/posts/{root['id']}/reactions/thumbsup", headers=alice_h
        ) as r:
            await r.read()
        await asyncio.sleep(0.6)

        # 5) ★ 봇이 자기 이름으로 게시 - 자기 발신 이벤트가 오는지 확인
        async with s.post(
            f"{base}/posts",
            headers={"Authorization": f"Bearer {bot_token}"},
            json={
                "channel_id": channel_id,
                "message": "[T-0.3] bridgebot 자기 발신 (루프 확인용)",
                "props": {"override_username": "Bob Lee (Slack)", "from_webhook": "true"},
            },
        ) as r:
            botpost = await r.json()
        ids["bot_post"] = botpost["id"]
        await asyncio.sleep(0.6)

        # 6) alice 삭제
        async with s.delete(f"{base}/posts/{reply['id']}", headers=alice_h) as r:
            await r.read()

        await s.post(f"{base}/users/logout", headers=alice_h)
    return ids


def report(events: list[dict[str, Any]], ids: dict[str, str], bot_id: str) -> None:
    kinds = Counter(e.get("event", "?") for e in events)

    print()
    print("=" * 78)
    print(" 수신한 이벤트 종류")
    print("=" * 78)
    for kind, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        mark = "★" if kind in WANTED else " "
        print(f"  {mark} {kind:<34} {n}건")

    print()
    print("=" * 78)
    print(" 필수 이벤트 도달 여부")
    print("=" * 78)
    for kind in sorted(WANTED):
        print(f"  {'✓' if kind in kinds else '✗'} {kind}")
    missing = WANTED - set(kinds)

    # ------------------------------------------------ posted 이벤트의 필드 확인
    print()
    print("=" * 78)
    print(" posted 이벤트에서 얻을 수 있는 필드")
    print("=" * 78)
    posted = [e for e in events if e.get("event") == "posted"]
    if posted:
        sample = posted[0]
        data = sample.get("data", {})
        post = json.loads(data["post"]) if isinstance(data.get("post"), str) else {}
        print(f"  data 키   : {sorted(data.keys())}")
        print(f"  post 키   : {sorted(post.keys())}")
        print()
        for k in ("id", "channel_id", "user_id", "root_id", "message", "type", "props"):
            print(f"    {k:<12} = {post.get(k)!r}")
        print(f"  broadcast : {sorted((sample.get('broadcast') or {}).keys())}")
    else:
        print("  posted 이벤트가 없어 확인 불가")

    # ------------------------------------------------ 자기 발신 여부 (핵심)
    print()
    print("=" * 78)
    print(" ★ 자기 발신 이벤트 수신 여부 (LoopGuard 설계 근거)")
    print("=" * 78)
    self_posts = []
    for e in posted:
        data = e.get("data", {})
        raw = data.get("post")
        if not isinstance(raw, str):
            continue
        post = json.loads(raw)
        if post.get("user_id") == bot_id:
            self_posts.append(post)

    if self_posts:
        p = self_posts[0]
        print(f"  ✓ 봇이 자기 게시물의 posted 이벤트를 받는다 ({len(self_posts)}건)")
        print("     → LoopGuard 방어 ①(자기 발신 무시)이 **필수**다.")
        print("     판별 가능한 필드:")
        print(f"       user_id      = {p.get('user_id')!r}  (== 봇 id)")
        print(f"       props.from_bot = {(p.get('props') or {}).get('from_bot')!r}")
        print(f"       props.from_webhook = {(p.get('props') or {}).get('from_webhook')!r}")
    else:
        print("  ✗ 자기 게시물 이벤트가 오지 않았다.")
        print("     → 방어 ①의 비중이 낮아지지만, 매핑 조회(②)는 여전히 필수다.")

    # ------------------------------------------------ 쓰레드
    print()
    print("=" * 78)
    print(" 쓰레드 식별 (root_id)")
    print("=" * 78)
    found_reply = False
    for e in posted:
        raw = e.get("data", {}).get("post")
        if not isinstance(raw, str):
            continue
        post = json.loads(raw)
        if post.get("id") == ids.get("alice_reply"):
            print(f"  ✓ 답글 이벤트의 root_id = {post.get('root_id')!r}")
            print(f"     기대값(원본 id)      = {ids.get('alice_post')!r}")
            found_reply = True
    if not found_reply:
        print("  ✗ 답글 이벤트를 찾지 못함")

    print()
    print("=" * 78)
    print(" 판정")
    print("=" * 78)
    if not missing:
        print("  ✓ 브릿지에 필요한 5종 이벤트 모두 수신 확인. T-0.3 통과.")
    else:
        print(f"  ✗ 누락: {sorted(missing)}")


def check_library() -> None:
    print()
    print("=" * 78)
    print(" mattermostautodriver websocket 진입점 조사")
    print("=" * 78)
    try:
        from mattermostautodriver import Driver
        from mattermostautodriver.websocket import Websocket
    except Exception as exc:
        print(f"  임포트 실패: {exc}")
        return
    try:
        sig = inspect.signature(Driver.init_websocket)
        print(f"  Driver.init_websocket{sig}")
        print(f"    코루틴 여부: {inspect.iscoroutinefunction(Driver.init_websocket)}")
    except Exception as exc:
        print(f"  init_websocket 조사 실패: {exc}")
    methods = [m for m in dir(Websocket) if not m.startswith("__")]
    print(f"  Websocket 공개 멤버: {methods}")


async def main() -> int:
    env = load_env()
    host = env.get("HOST_ADDR", "127.0.0.1")
    port = env.get("MM_A_PORT", "8071")
    token = env.get("MM_A_BOT_TOKEN")
    if not token:
        sys.exit("MM_A_BOT_TOKEN 이 없습니다.")

    base = f"http://{host}:{port}/api/v4"
    ws_url = f"ws://{host}:{port}/api/v4/websocket"

    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as s:
        async with s.get(f"{base}/users/me") as r:
            bot_id = (await r.json())["id"]
        async with s.get(f"{base}/teams/name/{TEAM}/channels/name/{CHANNEL}") as r:
            channel_id = (await r.json())["id"]

    print(f"봇 id     : {bot_id}")
    print(f"채널 id   : {channel_id}")
    print(f"WebSocket : {ws_url}")

    events: list[dict[str, Any]] = []
    ready = asyncio.Event()
    task = asyncio.create_task(listen(ws_url, token, events, ready))

    try:
        await asyncio.wait_for(ready.wait(), timeout=15)
        print("연결 확립 (hello 수신). 이벤트 유발 시작...\n")
    except TimeoutError:
        print("경고: hello 이벤트를 받지 못했습니다. 계속 진행합니다.")

    ids = await trigger(base, env, channel_id)
    await asyncio.sleep(2.0)  # 잔여 이벤트 수집

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    report(events, ids, bot_id)
    check_library()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
