# Channel Bridge

Mattermost 와 Slack 채널을 **양방향으로 잇는 릴레이**. 한쪽 채널의 메시지가
반대편 채널에 원 작성자 이름으로 나타나고, 쓰레드·편집·삭제·첨부가 함께 따라간다.

MM↔MM, MM↔Slack 양쪽 모두 실서버에서 검증되어 있다.

```
Mattermost A  ─┐                    ┌─  Mattermost B
               ├─  Channel Bridge  ─┤
Mattermost A  ─┘   (CIR + Router)   └─  Slack #channel
```

## 설계에서 중요한 것 세 가지

**1. 플랫폼 중립 이벤트 모델(CIR)이 중심에 있다.**
모든 어댑터는 수신 이벤트를 [`cir.Event`](src/chbridge/cir.py) 로 정규화하고,
송신 시 대상 플랫폼 형식으로 렌더링한다. Router 는 대상 Endpoint 의 플랫폼
종류를 알지 못한다. 그 결과 **MM↔MM 은 양쪽이 같은 구현체인 경우일 뿐이며
별도 코드가 발생하지 않는다.**

**2. 루프 방지가 무관용 지표다.**
브릿지가 전달한 메시지를 다시 원본으로 되돌리면 무한 증식한다. Mattermost 도
Slack 도 **봇이 자기 발신 이벤트를 받으므로** 자기 발신 필터는 선택이 아니다.
판정은 [`loopguard.py`](src/chbridge/loopguard.py) 한 곳에 모여 있고 단위
테스트로 고정된다.

| 방어 | 수단 | 뚫렸을 때 |
|---|---|---|
| ① 자기 발신 | `self_id()` 비교 | ②가 막는다 |
| ② 매핑 조회 | `message_links` 의 Replica 판정 | 루프 발생 |
| ③ 토폴로지 | 한 채널은 하나의 브릿지에만 | 기동 시 거부 |

자기 발신 판별 기준은 플랫폼마다 다르다. Mattermost 는 `post.user_id`,
Slack 은 `bot_id` 다 — 오버라이드 게시 시 Slack 의 `user` 필드가 비어버리기
때문이다. 게다가 **Slack 은 subtype 마다 `bot_id` 위치가 다르다.**

**3. 유실은 조용히 일어난다.**
WebSocket 이 끊긴 구간의 이벤트는 재생되지 않는다. 그래서 수신 루프는 재시작할
때마다 커서 이후를 백필하고, 그와 별개로 주기적 정합성 스윕이 돈다
([`app.py`](src/chbridge/app.py) 의 `_reconciler`). `slack_sdk` 는 내부적으로
자동 재연결하므로 Supervisor 가 재시작하지 않고, 그 구간이 로그에도 남지 않기
때문이다.

같은 이유로 **전달하지 못한 첨부는 반드시 안내 문구가 된다.** 크기 초과든
전송 실패든, 받는 쪽이 "첨부가 있었다"는 사실조차 모르는 상황을 만들지 않는다.

## 구성

| 모듈 | 역할 |
|---|---|
| [`cir`](src/chbridge/cir.py) | 플랫폼 중립 이벤트 모델. `event_inbox` JSONB 왕복 포함 |
| [`adapters`](src/chbridge/adapters) | Mattermost(REST+WS 자체 구현), Slack(Socket Mode) |
| [`loopguard`](src/chbridge/loopguard.py) | 루프 방지 3중 방어 |
| [`router`](src/chbridge/router.py) | 채널 이름 해석, Bridge/Endpoint 토폴로지 |
| [`relay`](src/chbridge/relay.py) | 한 방향 전달. 쓰레드 부모 변환, 첨부 순서 분기 |
| [`worker`](src/chbridge/worker.py) / [`store`](src/chbridge/store) | 재시도·DLQ, 백필 커서 |
| [`supervisor`](src/chbridge/supervisor.py) | 수신 루프 재시작. 재시작마다 백필 후 스트림 접속 |

### 첨부 처리 순서는 어댑터가 선언한다

플랫폼이 정반대 순서를 강제한다. Mattermost 는 `POST /posts` 에 `file_ids` 를
실어야 해서 게시 **전에** 올려야 하고, Slack 은 `chat.postMessage` 에 파일 id 를
붙일 수 없어 게시 **후에** `ts` 를 알아야 매달 수 있다. 그래서 `FileMode`
(`PRE_UPLOAD` / `POST_ATTACH`) 를 어댑터가 선언하고 Relay 가 그 값만 보고
분기한다 — Relay 는 여전히 플랫폼을 모른다.

바이트는 **브릿지를 통과만 하고 어디에도 저장되지 않는다.** 파일 크기가
브릿지의 자원 요구를 늘리지 않는다.

## 설정

자격증명은 YAML 에 담지 않는다. YAML 은 환경변수 **이름**만 참조한다.

```yaml
workspaces:
  - id: mm-a
    platform: mattermost
    alias: 본사MM
    base_url: http://mattermost.example.com
    token_env: MM_A_BOT_TOKEN        # 값이 아니라 변수 이름

  - id: slack-dev
    platform: slack
    alias: Slack
    token_env: SLACK_BOT_TOKEN
    app_token_env: SLACK_APP_TOKEN   # Socket Mode 용 App-Level Token

bridges:
  - id: demo
    name: MM <-> Slack
    endpoints:
      - workspace: mm-a
        team: bridge
        channel: bridge-1
      - workspace: slack-dev
        channel_id: C0XXXXXXXXX      # 이름 대신 id 를 쓰면 해석을 건너뛴다
    options:
      relay_edits: true
      relay_deletes: true
      relay_files: true
      relay_bot_messages: false
```

주요 환경변수는 다음과 같다.

| 변수 | 용도 |
|---|---|
| `CHBRIDGE_DATABASE_URL` | 브릿지 저장소 DSN |
| `CHBRIDGE_BRIDGES_FILE` | 브릿지 정의 YAML 경로 |
| `CHBRIDGE_MAX_ATTEMPTS` | 재시도 한계. 초과하면 DLQ |
| `CHBRIDGE_RECONCILE_SECONDS` | 정합성 스윕 간격 |

## 실행

Python 3.13 이상과 [uv](https://docs.astral.sh/uv/) 가 필요하다.

```bash
uv sync

uv run chbridge migrate   # 마이그레이션만 적용
uv run chbridge run       # 브릿지 기동
uv run chbridge status    # 브릿지·큐 상태 조회
uv run chbridge dlq       # DLQ 조회 / 재처리
```

개발 환경(Mattermost 2대 + 브릿지 DB)은 [`dev/`](dev/) 에 있다. 구성과 픽스처
생성 절차는 [dev/README.md](dev/README.md) 를 참고할 것.

> **주의** — 개발 환경의 MM 2대를 같은 호스트의 다른 포트로 열면 안 된다.
> 쿠키는 포트를 스코프에 포함하지 않으므로(RFC 6265 §8.5) 한쪽에 로그인하는
> 순간 다른 쪽 세션이 밀려난다. `dev/docker-compose.yml` 상단 주석 참고.

## 개발

```bash
uv run pytest      # 단위 테스트
uv run ruff check  # 린트
uv run mypy src    # 타입 검사
```

테스트는 **회귀 방지가 목적**이다. 각 파일 상단에 "이 파일이 막는 것"이 적혀
있다. 특히 Slack 의 subtype 별 `bot_id` 위치, 첨부 게시 순서, 표시 이름 캐시
만료는 실제로 한 번씩 깨졌던 지점이다.

## 알려진 제약

- **Slack 첨부 업로드는 파일 크기를 알아야 한다.** `files.getUploadURLExternal`
  이 `length` 를 요구한다. 원본이 크기를 알려주지 않으면 버퍼링 대신 실패로
  처리한다 — 모르는 크기를 메모리에 담는 쪽이 더 위험하다.
- **리액션 전달은 미구현**이다. 조용히 무시하지 않고 로그를 남긴다.
- **브릿지 생성 이전의 과거 대화는 백필하지 않는다.** 첫 기동은 현재 시점부터
  시작한다.
- 대상 플랫폼의 크기 제한을 넘는 첨부는 전달되지 않고 안내 문구가 된다.
  링크 대체나 외부 저장소는 도입하지 않았다.
