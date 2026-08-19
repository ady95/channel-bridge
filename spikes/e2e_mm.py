"""MM↔MM 엔드투엔드 검증. (Phase 1 완료 조건)

브릿지가 실행 중인 상태에서 실제 Mattermost 두 대를 상대로
텍스트·양방향·쓰레드·편집·삭제·루프부재를 확인한다.

    # 터미널 1
    uv run python -m chbridge run
    # 터미널 2
    uv run python spikes/e2e_mm.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

DEV_DIR = Path(__file__).resolve().parent.parent / "dev"
TEAM = "bridge"
CHANNEL = "bridge-1"
PASSWORD = "Bridge-Test-1234"
TAG = f"E2E-{int(time.time()) % 100000}"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
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
        path = DEV_DIR / name
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


class MM:
    def __init__(self, session: aiohttp.ClientSession, base: str, label: str) -> None:
        self.s = session
        self.api = f"{base}/api/v4"
        self.label = label
        self.channel_id = ""
        self._tokens: dict[str, str] = {}

    async def login(self, login_id: str) -> str:
        if login_id in self._tokens:
            return self._tokens[login_id]
        async with self.s.post(
            f"{self.api}/users/login", json={"login_id": login_id, "password": PASSWORD}
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"{self.label} {login_id} 로그인 실패 {r.status}")
            token = r.headers.get("Token", "")
        self._tokens[login_id] = token
        return token

    def hdr(self, login_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens[login_id]}"}

    async def resolve(self, admin: str) -> None:
        async with self.s.get(
            f"{self.api}/teams/name/{TEAM}/channels/name/{CHANNEL}", headers=self.hdr(admin)
        ) as r:
            self.channel_id = (await r.json())["id"]

    async def post(self, who: str, text: str, *, root_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"channel_id": self.channel_id, "message": text}
        if root_id:
            body["root_id"] = root_id
        async with self.s.post(f"{self.api}/posts", json=body, headers=self.hdr(who)) as r:
            if r.status != 201:
                raise RuntimeError(f"{self.label} 게시 실패 {r.status}: {await r.text()}")
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

    async def posts(self, admin: str, *, limit: int = 60) -> list[dict[str, Any]]:
        async with self.s.get(
            f"{self.api}/channels/{self.channel_id}/posts",
            params={"per_page": str(limit)},
            headers=self.hdr(admin),
        ) as r:
            page = await r.json()
        return [page["posts"][pid] for pid in page["order"]]

    async def wait_for(
        self, admin: str, needle: str, *, timeout: float = 25.0
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for post in await self.posts(admin):
                if needle in str(post.get("message", "")) and not post.get("delete_at"):
                    return post
            await asyncio.sleep(0.7)
        return None

    async def wait_gone(self, admin: str, needle: str, *, timeout: float = 25.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hit = [
                p
                for p in await self.posts(admin)
                if needle in str(p.get("message", "")) and not p.get("delete_at")
            ]
            if not hit:
                return True
            await asyncio.sleep(0.7)
        return False

    async def count(self, admin: str, needle: str) -> int:
        return sum(
            1
            for p in await self.posts(admin)
            if needle in str(p.get("message", "")) and not p.get("delete_at")
        )


async def main() -> int:
    env = load_env()
    host = env.get("HOST_ADDR", "127.0.0.1")

    async with aiohttp.ClientSession() as s:
        a = MM(s, f"http://{host}:{env['MM_A_PORT']}", "mm-a")
        b = MM(s, f"http://{host}:{env['MM_B_PORT']}", "mm-b")

        for mm, users in (
            (a, ("admin@test.local", "alice@test.local")),
            (b, ("admin@test.local", "carol@test.local")),
        ):
            for u in users:
                await mm.login(u)
            await mm.resolve("admin@test.local")

        print(f"\n태그: {TAG}")
        print(f"mm-a 채널: {a.channel_id}")
        print(f"mm-b 채널: {b.channel_id}\n")

        print("=" * 72)
        print(" 1. A -> B 텍스트 전달 + 표시 이름 오버라이드")
        print("=" * 72)
        origin = await a.post("alice@test.local", f"{TAG} A에서 B로")
        replica = await b.wait_for("admin@test.local", f"{TAG} A에서 B로")
        if check(replica is not None, "B 에 복제 메시지 도착"):
            assert replica is not None
            props = replica.get("props") or {}
            name = str(props.get("override_username") or "")
            check(bool(name), f"표시 이름 오버라이드 적용: {name!r}")
            check("본사MM" in name, f"출처 별칭 표기 포함 (FR-2.3): {name!r}")
            check(props.get("from_webhook") == "true", "from_webhook props 설정")

        print()
        print("=" * 72)
        print(" 2. B -> A 역방향 전달")
        print("=" * 72)
        await b.post("carol@test.local", f"{TAG} B에서 A로")
        back = await a.wait_for("admin@test.local", f"{TAG} B에서 A로")
        if check(back is not None, "A 에 복제 메시지 도착 (양방향 확인)"):
            assert back is not None
            name = str((back.get("props") or {}).get("override_username") or "")
            check("파트너MM" in name, f"출처 별칭: {name!r}")

        print()
        print("=" * 72)
        print(" 3. 루프 부재 (NFR-2 무관용 지표)")
        print("=" * 72)
        await asyncio.sleep(8)
        a1 = await a.count("admin@test.local", f"{TAG} A에서 B로")
        b1 = await b.count("admin@test.local", f"{TAG} A에서 B로")
        a2 = await a.count("admin@test.local", f"{TAG} B에서 A로")
        b2 = await b.count("admin@test.local", f"{TAG} B에서 A로")
        print(f"      'A에서 B로' -> mm-a {a1}건, mm-b {b1}건")
        print(f"      'B에서 A로' -> mm-a {a2}건, mm-b {b2}건")
        check(a1 == 1 and b1 == 1, "A->B 메시지가 각 채널에 정확히 1건", f"{a1}/{b1}")
        check(a2 == 1 and b2 == 1, "B->A 메시지가 각 채널에 정확히 1건", f"{a2}/{b2}")

        print()
        print("=" * 72)
        print(" 4. 쓰레드 전달 (PRD 5.7)")
        print("=" * 72)
        await a.post("alice@test.local", f"{TAG} 쓰레드 답글", root_id=origin["id"])
        threaded = await b.wait_for("admin@test.local", f"{TAG} 쓰레드 답글")
        if check(threaded is not None, "B 에 답글 도착"):
            assert threaded is not None and replica is not None
            check(
                threaded.get("root_id") == replica["id"],
                "답글의 root_id 가 B 쪽 원본 복제본을 가리킴",
                f"root_id={threaded.get('root_id')!r} 기대={replica['id']!r}",
            )

        print()
        print("=" * 72)
        print(" 5. 편집 전달 (FR-6.1)")
        print("=" * 72)
        await a.patch("alice@test.local", origin["id"], f"{TAG} A에서 B로 (편집됨)")
        edited = await b.wait_for("admin@test.local", f"{TAG} A에서 B로 (편집됨)")
        check(edited is not None, "B 복제본 내용이 갱신됨")
        if edited is not None and replica is not None:
            check(edited["id"] == replica["id"], "새 메시지가 아니라 기존 복제본이 수정됨")

        print()
        print("=" * 72)
        print(" 6. 삭제 전달 (FR-6.2)")
        print("=" * 72)
        await a.delete("alice@test.local", origin["id"])
        check(
            await b.wait_gone("admin@test.local", f"{TAG} A에서 B로 (편집됨)"),
            "B 복제본이 삭제됨",
        )

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
