"""MM<->Slack 엔드투엔드 검증. (T-2.8 / Phase 2 완료 조건)

MM -> Slack 방향은 전부 자동 검증한다. 포맷 변환·표시 이름·쓰레드·편집·삭제·
루프부재를 확인한다.

Slack -> MM 방향은 자동화할 수 없다. 우리가 가진 것은 브릿지 봇 토큰뿐이고,
그 봇으로 게시하면 LoopGuard 가 자기 발신으로 정확히 걸러낸다(그게 올바른
동작이다). 사람이 Slack 에 입력해야 하므로 마지막 단계에서 대기·확인한다.

    uv run python spikes/e2e_slack.py            # MM -> Slack 자동 검증
    uv run python spikes/e2e_slack.py --manual   # Slack -> MM 까지 포함
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

DEV_DIR = Path(__file__).resolve().parent.parent / "dev"
MM_TEAM = "bridge"
MM_CHANNEL = "bridge-2"
PASSWORD = "Bridge-Test-1234"
TAG = f"S2E-{int(time.time()) % 100000}"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"
failures = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    global failures
    if ok:
        print(f"  {PASS} {label}")
    else:
        failures += 1
        print(f"  {FAIL} {label}" + (f"\n      {detail}" if detail else ""))
    return ok


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for name in (".env", ".tokens.env"):
        for raw in (DEV_DIR / name).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


class Mattermost:
    def __init__(self, session: aiohttp.ClientSession, base: str) -> None:
        self.s = session
        self.api = f"{base}/api/v4"
        self.tokens: dict[str, str] = {}
        self.channel_id = ""

    async def login(self, login_id: str) -> None:
        async with self.s.post(
            f"{self.api}/users/login", json={"login_id": login_id, "password": PASSWORD}
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"{login_id} 로그인 실패 {r.status}")
            self.tokens[login_id] = r.headers.get("Token", "")

    def hdr(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    async def resolve(self, who: str) -> None:
        async with self.s.get(
            f"{self.api}/teams/name/{MM_TEAM}/channels/name/{MM_CHANNEL}", headers=self.hdr(who)
        ) as r:
            self.channel_id = (await r.json())["id"]

    async def post(self, who: str, text: str, root_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"channel_id": self.channel_id, "message": text}
        if root_id:
            body["root_id"] = root_id
        async with self.s.post(f"{self.api}/posts", json=body, headers=self.hdr(who)) as r:
            if r.status != 201:
                raise RuntimeError(f"게시 실패 {r.status}: {await r.text()}")
            result: dict[str, Any] = await r.json()
            return result

    async def patch(self, who: str, post_id: str, text: str) -> None:
        async with self.s.put(
            f"{self.api}/posts/{post_id}/patch", json={"message": text}, headers=self.hdr(who)
        ) as r:
            await r.read()

    async def delete(self, who: str, post_id: str) -> None:
        async with self.s.delete(f"{self.api}/posts/{post_id}", headers=self.hdr(who)) as r:
            await r.read()

    async def posts(self, who: str) -> list[dict[str, Any]]:
        async with self.s.get(
            f"{self.api}/channels/{self.channel_id}/posts",
            params={"per_page": "60"},
            headers=self.hdr(who),
        ) as r:
            page = await r.json()
        return [page["posts"][pid] for pid in page["order"]]

    async def wait_for(self, who: str, needle: str, timeout: float = 30.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for post in await self.posts(who):
                if needle in str(post.get("message", "")) and not post.get("delete_at"):
                    return post
            await asyncio.sleep(0.8)
        return None


class Slack:
    def __init__(self, session: aiohttp.ClientSession, token: str, channel: str) -> None:
        self.s = session
        self.token = token
        self.channel = channel

    async def call(self, method: str, **params: str) -> dict[str, Any]:
        async with self.s.get(
            f"https://slack.com/api/{method}",
            params={"channel": self.channel, **params},
            headers={"Authorization": f"Bearer {self.token}"},
        ) as r:
            data: dict[str, Any] = await r.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack {method} 실패: {data.get('error')}")
        return data

    async def history(self, limit: int = 40) -> list[dict[str, Any]]:
        data = await self.call("conversations.history", limit=str(limit))
        return list(data.get("messages") or [])

    async def replies(self, ts: str) -> list[dict[str, Any]]:
        data = await self.call("conversations.replies", ts=ts, limit="30")
        return list(data.get("messages") or [])

    async def wait_for(self, needle: str, timeout: float = 40.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in await self.history():
                if needle in str(message.get("text", "")):
                    return message
            await asyncio.sleep(1.2)
        return None

    async def wait_gone(self, needle: str, timeout: float = 40.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(needle in str(m.get("text", "")) for m in await self.history()):
                return True
            await asyncio.sleep(1.2)
        return False

    async def count(self, needle: str) -> int:
        return sum(1 for m in await self.history() if needle in str(m.get("text", "")))


async def main() -> int:
    env = load_env()
    manual = "--manual" in sys.argv
    host = env.get("HOST_ADDR", "127.0.0.1")

    async with aiohttp.ClientSession() as session:
        mm = Mattermost(session, f"http://{host}:{env['MM_A_PORT']}")
        for user in ("admin@test.local", "alice@test.local"):
            await mm.login(user)
        await mm.resolve("admin@test.local")
        slack = Slack(session, env["SLACK_BOT_TOKEN"], env["SLACK_TEST_CHANNEL"])

        print(f"\n태그: {TAG}")
        print(f"MM 채널   : {MM_TEAM}/{MM_CHANNEL} ({mm.channel_id})")
        print(f"Slack 채널: {slack.channel}\n")

        # ---------------------------------------------------- 1. 텍스트 + 포맷 변환
        print("=" * 72)
        print(" 1. MM -> Slack 전달 + 포맷 변환 (PRD 5.4)")
        print("=" * 72)
        rich = f"{TAG} **굵게** 와 _기울임_ 와 [링크](https://example.com) 와 `코드`"
        origin = await mm.post("alice@test.local", rich)
        delivered = await slack.wait_for(TAG)

        if check(delivered is not None, "Slack 에 메시지 도착"):
            assert delivered is not None
            text = str(delivered.get("text", ""))
            print(f"      수신 텍스트: {text}")
            check(
                delivered.get("username", "") == "Alice Kim (본사MM)",
                f"표시 이름 오버라이드: {delivered.get('username')!r}",
            )
            check("*굵게*" in text, "굵게 변환 (**x** -> *x*)", text)
            check("_기울임_" in text, "기울임 유지 (_x_)", text)
            check("<https://example.com|링크>" in text, "링크 변환", text)
            check("`코드`" in text, "인라인 코드 보존", text)

        # ---------------------------------------------------- 2. 루프 부재
        print()
        print("=" * 72)
        print(" 2. 루프 부재 (NFR-2 무관용 지표)")
        print("=" * 72)
        await asyncio.sleep(10)
        slack_n = await slack.count(TAG)
        mm_n = sum(
            1
            for p in await mm.posts("admin@test.local")
            if TAG in str(p.get("message", "")) and not p.get("delete_at")
        )
        print(f"      MM {mm_n}건 / Slack {slack_n}건")
        check(slack_n == 1, "Slack 에 정확히 1건", f"{slack_n}건")
        check(mm_n == 1, "MM 에 정확히 1건", f"{mm_n}건")

        # ---------------------------------------------------- 3. 쓰레드
        print()
        print("=" * 72)
        print(" 3. 쓰레드 전달 (PRD 5.7)")
        print("=" * 72)
        thread_tag = f"{TAG}-답글"
        await mm.post("alice@test.local", thread_tag, root_id=origin["id"])

        # ★ conversations.history 는 쓰레드 답글을 포함하지 않는다.
        #   답글은 conversations.replies 로 조회해야 한다. 백필 구현에서도
        #   같은 이유로 replies 를 별도 호출한다 (SlackAdapter.backfill).
        assert delivered is not None
        parent_ts = str(delivered["ts"])
        reply = None
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            for message in await slack.replies(parent_ts):
                if thread_tag in str(message.get("text", "")):
                    reply = message
                    break
            if reply:
                break
            await asyncio.sleep(1.2)

        if check(reply is not None, "Slack 쓰레드에 답글 도착"):
            assert reply is not None
            check(
                reply.get("thread_ts") == parent_ts,
                "thread_ts 가 Slack 쪽 원본을 지목",
                f"thread_ts={reply.get('thread_ts')!r} 기대={parent_ts!r}",
            )

        # ---------------------------------------------------- 4. 편집
        print()
        print("=" * 72)
        print(" 4. 편집 전달 (FR-6.1)")
        print("=" * 72)
        await mm.patch("alice@test.local", origin["id"], f"{TAG} **편집됨**")
        edited = None
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            for message in await slack.history():
                if TAG in str(message.get("text", "")) and "편집됨" in str(message.get("text", "")):
                    edited = message
                    break
            if edited:
                break
            await asyncio.sleep(1.2)
        if check(edited is not None, "Slack 복제본이 갱신됨"):
            assert edited is not None and delivered is not None
            check(edited.get("ts") == delivered.get("ts"), "기존 메시지를 수정 (새 게시 아님)")
            check("*편집됨*" in str(edited.get("text", "")), "편집 내용도 포맷 변환됨")

        # ---------------------------------------------------- 5. 삭제
        print()
        print("=" * 72)
        print(" 5. 삭제 전달 (FR-6.2)")
        print("=" * 72)
        await mm.delete("alice@test.local", origin["id"])
        check(await slack.wait_gone(f"{TAG} *편집됨*"), "Slack 복제본이 삭제됨")

        # ---------------------------------------------------- 6. Slack -> MM
        print()
        print("=" * 72)
        print(" 6. Slack -> MM 역방향")
        print("=" * 72)
        if not manual:
            print(f"  {WARN} 자동화 불가 - 건너뜀")
            print("      우리가 가진 것은 브릿지 봇 토큰뿐이고, 그 봇으로 게시하면")
            print("      LoopGuard 가 자기 발신으로 정확히 걸러낸다 (올바른 동작).")
            print("      --manual 로 실행하면 직접 입력해 확인할 수 있다.")
        else:
            needle = f"{TAG}-역방향"
            print("  Slack #test 채널에 아래 문구를 포함한 메시지를 입력하세요 (90초 대기):")
            print(f"\n      {needle} **굵게** 테스트\n")
            found = await mm.wait_for("admin@test.local", needle, timeout=90)
            if check(found is not None, "MM 에 Slack 메시지 도착"):
                assert found is not None
                name = str((found.get("props") or {}).get("override_username") or "")
                text = str(found.get("message", ""))
                print(f"      표시 이름: {name!r}")
                print(f"      본문     : {text!r}")
                check("(Slack)" in name, f"출처 별칭 표기: {name!r}")
                check("**굵게**" in text, "mrkdwn -> Markdown 변환", text)

        # ---------------------------------------------------- 결과
        print()
        print("=" * 72)
        print(" 결과")
        print("=" * 72)
        if failures == 0:
            print("  \033[32m전체 통과\033[0m")
        else:
            print(f"  \033[31m실패 {failures}건\033[0m")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
