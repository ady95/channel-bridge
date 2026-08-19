"""T-0.2b : Bot 게시물이 incoming webhook 게시물과 구조적으로 동일한지 증명

spike_override.py 로 "서버가 props를 걸러내지 않는다"는 것은 확인했다.
남은 질문은 "클라이언트가 그 props를 렌더링에 쓰는가"이다.

육안 확인 대신 등가성으로 증명한다.
Mattermost 웹앱은 게시물의 props만 보고 표시 이름을 결정한다. 따라서
  Bot이 만든 게시물의 props == 실제 webhook이 만든 게시물의 props
이면 웹앱은 둘을 구분할 수 없고, 렌더링도 반드시 같다.

    uv run python spikes/spike_webhook_equiv.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import aiohttp

DEV_DIR = Path(__file__).resolve().parent.parent / "dev"
TEAM = "bridge"
CHANNEL = "bridge-1"
ADMIN_LOGIN = "admin@test.local"
ADMIN_PASSWORD = "Bridge-Test-1234"

FAKE_NAME = "Alice Kim (Slack)"
FAKE_ICON = "https://www.mattermost.org/wp-content/uploads/2016/04/icon.png"

# 표시 이름 결정에 관여하는 props 키만 비교한다.
RENDER_KEYS = ("override_username", "override_icon_url", "from_webhook", "webhook_display_name")


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


async def main() -> int:
    env = load_env()
    host = env.get("HOST_ADDR", "127.0.0.1")
    port = env.get("MM_A_PORT", "8071")
    bot_token = env.get("MM_A_BOT_TOKEN")
    if not bot_token:
        sys.exit("MM_A_BOT_TOKEN 이 없습니다.")

    root = f"http://{host}:{port}"
    base = f"{root}/api/v4"

    async with aiohttp.ClientSession() as s:
        # ---------------------------------------------------------- 관리자 로그인
        async with s.post(
            f"{base}/users/login",
            json={"login_id": ADMIN_LOGIN, "password": ADMIN_PASSWORD},
        ) as r:
            if r.status != 200:
                sys.exit(f"관리자 로그인 실패 HTTP {r.status}: {await r.text()}")
            admin_token = r.headers.get("Token", "")
        admin_h = {"Authorization": f"Bearer {admin_token}"}

        # ---------------------------------------------------------- 채널
        async with s.get(f"{base}/teams/name/{TEAM}/channels/name/{CHANNEL}", headers=admin_h) as r:
            channel_id = (await r.json())["id"]

        # ---------------------------------------------------------- webhook 생성
        async with s.post(
            f"{base}/hooks/incoming",
            headers=admin_h,
            json={"channel_id": channel_id, "display_name": "T-0.2b 등가성 검증"},
        ) as r:
            if r.status != 201:
                sys.exit(f"incoming webhook 생성 실패 HTTP {r.status}: {await r.text()}")
            hook = await r.json()
        hook_id = hook["id"]
        print(f"incoming webhook 생성: {hook_id}")

        try:
            # ------------------------------------------------ webhook 으로 게시
            async with s.post(
                f"{root}/hooks/{hook_id}",
                json={
                    "text": "[T-0.2b] 실제 webhook 이 만든 게시물",
                    "username": FAKE_NAME,
                    "icon_url": FAKE_ICON,
                },
            ) as r:
                if r.status != 200:
                    sys.exit(f"webhook 게시 실패 HTTP {r.status}: {await r.text()}")

            # ------------------------------------------------ Bot 으로 게시
            bot_h = {"Authorization": f"Bearer {bot_token}"}
            async with s.post(
                f"{base}/posts",
                headers=bot_h,
                json={
                    "channel_id": channel_id,
                    "message": "[T-0.2b] Bot 이 props 로 만든 게시물",
                    "props": {
                        "override_username": FAKE_NAME,
                        "override_icon_url": FAKE_ICON,
                        "from_webhook": "true",
                    },
                },
            ) as r:
                if r.status != 201:
                    sys.exit(f"Bot 게시 실패 HTTP {r.status}: {await r.text()}")
                bot_post_id = (await r.json())["id"]

            # ------------------------------------------------ 되읽기
            async with s.get(
                f"{base}/channels/{channel_id}/posts?per_page=10", headers=admin_h
            ) as r:
                page = await r.json()

            posts: list[dict[str, Any]] = [page["posts"][pid] for pid in page["order"]]
            hook_post = next((p for p in posts if "실제 webhook" in p.get("message", "")), None)
            bot_post = next((p for p in posts if p["id"] == bot_post_id), None)

            if hook_post is None or bot_post is None:
                sys.exit("게시물 조회 실패")

            hp = hook_post.get("props") or {}
            bp = bot_post.get("props") or {}

            print()
            print("=" * 78)
            print(" props 비교")
            print("=" * 78)
            print(f"  {'키':<24} {'webhook 게시물':<26} {'Bot 게시물':<26}")
            print("  " + "-" * 74)
            allkeys = sorted(set(hp) | set(bp))
            for k in allkeys:
                hv = str(hp.get(k, "—"))[:24]
                bv = str(bp.get(k, "—"))[:24]
                mark = " " if hp.get(k) == bp.get(k) else "≠"
                print(f"  {k:<24} {hv:<26} {bv:<26} {mark}")

            print()
            print(f"  webhook 게시물 user_id : {hook_post.get('user_id')}")
            print(f"  Bot     게시물 user_id : {bot_post.get('user_id')}")
            print(f"  webhook 게시물 type    : {hook_post.get('type') or '(일반)'}")
            print(f"  Bot     게시물 type    : {bot_post.get('type') or '(일반)'}")

            # ------------------------------------------------ 판정
            print()
            print("=" * 78)
            print(" 판정")
            print("=" * 78)
            diffs = [k for k in RENDER_KEYS if hp.get(k) != bp.get(k)]
            if not diffs:
                print("  ✓ 렌더링에 관여하는 props 가 webhook 게시물과 완전히 동일하다.")
                print("    웹앱은 두 게시물을 구분할 수 없으므로 표시 이름도 동일하게 렌더링된다.")
                print("    → D-4 채택안(Bot 오버라이드) 확정 가능. shadow 사용자 불필요.")
            else:
                print(f"  ✗ 차이 발견: {diffs}")
                for k in diffs:
                    print(f"      {k}: webhook={hp.get(k)!r}  bot={bp.get(k)!r}")
                print("    → 차이가 렌더링에 영향하는지 육안 확인 필요.")

            print()
            print(f"  육안 재확인: {root}/{TEAM}/channels/{CHANNEL}")

        finally:
            async with s.delete(f"{base}/hooks/incoming/{hook_id}", headers=admin_h) as r:
                print(f"\nwebhook 정리: HTTP {r.status}")
            await s.post(f"{base}/users/logout", headers=admin_h)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
