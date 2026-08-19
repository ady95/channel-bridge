"""T-0.2 : Mattermost 표시 이름/아이콘 오버라이드 실측  (리스크 R-1)

PRD 5.3의 채택안은 "Bot 1개가 전송하되 표시 이름을 원 작성자로 오버라이드한다"이다.
알려진 함정은 `props.override_username` 이 `props.from_webhook = "true"` 와
함께 설정되어야 적용된다는 것이다. Bot 토큰 REST 호출만으로 동작하지 않으면
D-4(사용자 표현)가 뒤집히고 shadow 사용자 방식으로 선회해야 한다.

4가지 조합을 실제로 게시하고, 저장된 props를 되읽어 무엇이 살아남는지 확인한다.

    uv run python spikes/spike_override.py

마지막에 출력되는 URL을 브라우저로 열어 **실제 렌더링을 눈으로 확인**해야 한다.
props가 저장되어도 클라이언트가 무시할 수 있으므로 육안 확인이 최종 판정이다.
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

FAKE_NAME = "Alice Kim (Slack)"
FAKE_ICON = "https://www.mattermost.org/wp-content/uploads/2016/04/icon.png"


def load_env() -> dict[str, str]:
    """dev/.env 와 dev/.tokens.env 를 읽는다."""
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


# 실측할 조합. (이름, props)
CASES: list[tuple[str, dict[str, Any]]] = [
    (
        "1. props 없음 (기준선)",
        {},
    ),
    (
        "2. override_username 단독",
        {"override_username": FAKE_NAME},
    ),
    (
        "3. override_username + from_webhook",
        {"override_username": FAKE_NAME, "from_webhook": "true"},
    ),
    (
        "4. override_username + override_icon_url + from_webhook",
        {
            "override_username": FAKE_NAME,
            "override_icon_url": FAKE_ICON,
            "from_webhook": "true",
        },
    ),
]


async def main() -> int:
    env = load_env()
    host = env.get("HOST_ADDR", "127.0.0.1")
    port = env.get("MM_A_PORT", "8071")
    token = env.get("MM_A_BOT_TOKEN")
    if not token:
        sys.exit("MM_A_BOT_TOKEN 이 없습니다. dev/fixtures.sh 를 먼저 실행하세요.")

    base = f"http://{host}:{port}/api/v4"
    headers = {"Authorization": f"Bearer {token}"}

    async with aiohttp.ClientSession(headers=headers) as s:
        # --- 봇 자신 확인 ---
        async with s.get(f"{base}/users/me") as r:
            me = await r.json()
        print(f"봇 계정   : {me['username']} (id={me['id']}, is_bot={me.get('is_bot')})")

        # --- 대상 채널 확인 ---
        url = f"{base}/teams/name/{TEAM}/channels/name/{CHANNEL}"
        async with s.get(url) as r:
            if r.status != 200:
                sys.exit(f"채널 조회 실패 HTTP {r.status}: {await r.text()}")
            channel = await r.json()
        channel_id = channel["id"]
        print(f"대상 채널 : {TEAM}/{CHANNEL} (id={channel_id})")
        print()

        results: list[tuple[str, str, dict[str, Any], int]] = []

        for label, props in CASES:
            body: dict[str, Any] = {
                "channel_id": channel_id,
                "message": f"[T-0.2 실측] {label}",
            }
            if props:
                body["props"] = props

            async with s.post(f"{base}/posts", json=body) as r:
                status = r.status
                created = await r.json()

            if status != 201:
                print(f"  {label}\n    게시 실패 HTTP {status}: {created}")
                results.append((label, "게시실패", {}, status))
                continue

            post_id = created["id"]

            # 저장된 결과를 되읽는다. 서버가 props를 걸러냈는지 확인하기 위함.
            async with s.get(f"{base}/posts/{post_id}") as r:
                stored = await r.json()

            stored_props = stored.get("props") or {}
            results.append((label, post_id, stored_props, status))

        # ------------------------------------------------------------ 보고
        print("=" * 78)
        print(" 저장된 props (서버가 걸러냈는지 확인)")
        print("=" * 78)
        keys = ("override_username", "override_icon_url", "from_webhook")
        print(f"  {'조합':<48} " + " ".join(f"{k.replace('override_', 'ov_'):<18}" for k in keys))
        print("  " + "-" * 74)
        for label, _pid, props, _st in results:
            cells = []
            for k in keys:
                v = props.get(k)
                cells.append(f"{'—' if v is None else str(v)[:16]:<18}")
            print(f"  {label:<48} " + " ".join(cells))

        print()
        print("=" * 78)
        print(" 판정")
        print("=" * 78)

        case2 = next((p for lbl, _, p, _ in results if lbl.startswith("2.")), {})
        case3 = next((p for lbl, _, p, _ in results if lbl.startswith("3.")), {})

        c2_ok = case2.get("override_username") == FAKE_NAME
        c3_ok = case3.get("override_username") == FAKE_NAME
        c3_wh = case3.get("from_webhook") == "true"

        if c2_ok and not c3_wh:
            print("  override_username 이 from_webhook 없이도 저장됨.")
        if c3_ok and c3_wh:
            print("  ✓ override_username + from_webhook 조합이 그대로 저장됨.")
            print("    → Bot 토큰 REST 호출로 오버라이드 가능. D-4 채택안 유지 가능성 높음.")
        elif not c3_ok:
            print("  ✗ override_username 이 서버에서 제거됨.")
            print("    → D-4 재검토 필요. shadow 사용자 방식 선회 검토.")

        if not c2_ok and c3_ok:
            print("  ! from_webhook='true' 가 반드시 필요함이 확인됨.")
            print("    → 어댑터는 모든 복제 메시지에 from_webhook 을 함께 넣어야 한다.")
        elif c2_ok and c3_ok:
            print("  ! from_webhook 없이도 props가 저장되지만, 렌더링 적용 여부는")
            print("    아래 URL에서 육안 확인이 필요하다.")

        print()
        print("=" * 78)
        print(" 육안 확인 (최종 판정)")
        print("=" * 78)
        print("  아래 URL을 브라우저로 열고 4개 메시지의 '표시 이름'을 확인하세요.")
        print(f"    http://{host}:{port}/{TEAM}/channels/{CHANNEL}")
        print("    로그인: admin@test.local / Bridge-Test-1234")
        print()
        print(f"  기대: 3번과 4번이 '{FAKE_NAME}' 로 보이고, 1번은 'bridgebot' 으로 보인다.")
        print("  2번이 어떻게 보이는지가 from_webhook 필요 여부의 실질 판정이다.")
        print("  4번은 프로필 아이콘까지 바뀌어야 한다.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
