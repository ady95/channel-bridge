# Channel Bridge - 개발/테스트 환경

PRD의 Phase 0/1 검증에 필요한 Mattermost 2대와 브릿지 저장소를 띄운다.

## 왜 Team Edition 고정인가

`mattermost/mattermost-team-edition` 이미지를 명시적으로 사용한다.
`mattermost-enterprise-edition` 이미지는 라이선스 없이 실행하면 **Entry 모드**로
동작하며 Shared Channels 등 유료 기능이 켜진다. 그 환경에서 개발하면
Team Edition에 없는 기능에 의존하는 코드가 만들어질 수 있다.

| 에디션 | Shared Channels | 메시지 히스토리 |
|---|---|---|
| Team Edition (본 환경) | 없음 | 무제한 |
| Entry (무료, EE 이미지 무라이선스) | 있음 | 10,000건 제한 |

## 구성

| 서비스 | 역할 | 접속 |
|---|---|---|
| `mm-a` | Mattermost A | http://192.168.0.136:8071 |
| `mm-b` | Mattermost B (MM↔MM 상대편) | http://192.168.0.136:8072 |
| `db-a`, `db-b` | 각 MM 전용 PostgreSQL | 컨테이너 내부만 |
| `bridge-db` | 브릿지 저장소 (MessageLink 등) | `192.168.0.136:5433` |

기존 8065(Entry 테스트용)와 5432(`taxchat_pg`)는 건드리지 않는다.

## 사용법

```bash
docker compose up -d          # 기동
docker compose ps             # 상태
docker compose logs -f mm-a   # 로그
docker compose down           # 정지 (데이터 유지)
docker compose down -v        # 완전 초기화 (데이터 삭제)
```

`down -v`로 깨끗한 상태에서 반복 테스트할 수 있도록 bind mount 대신
named volume을 쓴다.

## 브릿지 관련 설정

`docker-compose.yml`의 `x-mm-env`에 아래 항목이 이미 켜져 있다.
System Console에서 따로 만질 필요 없다.

- `EnablePostUsernameOverride`, `EnablePostIconOverride` — 원 작성자 표시
- `EnableUserAccessTokens`, `EnableBotAccountCreation` — 브릿지 Bot 인증
- `EnableIncomingWebhooks`, `EnableOutgoingWebhooks`
- `EnableLocalMode` — 아래 mmctl 자동화용
- `MaxFileSize` = 100MB — Slack(1GB)과의 차이를 개발 중에도 재현

## mmctl 자동화 (LocalMode)

LocalMode가 켜져 있어 인증 없이 관리 명령을 실행할 수 있다.
테스트 픽스처(팀·채널·사용자 생성)를 스크립트로 만들 때 사용한다.

```bash
docker compose exec mm-a mmctl --local user create \
  --email a@test.local --username usera --password 'Test-1234'

docker compose exec mm-a mmctl --local team create --name t1 --display-name "Team 1"
docker compose exec mm-a mmctl --local channel create --team t1 --name c1 --display-name "Ch 1"
```

## 네트워크 단절 테스트 (PRD 5.9 sync_cursor 검증)

재연결 백필이 동작하는지 확인할 때 쓴다.

```bash
NET=$(docker network ls --format '{{.Name}}' | grep chbridge-dev)
docker network disconnect $NET chbridge-dev-mm-b-1   # 단절
# 이 구간에 mm-a 쪽에 메시지 투입
docker network connect $NET chbridge-dev-mm-b-1      # 재연결 -> 백필 확인
```

## 최초 설정

각 인스턴스에 브라우저로 접속해 최초 관리자 계정을 만든다.
`EnableOpenServer=true`이므로 가입 제한이 없다.
이메일 인증과 메일 발송은 꺼져 있다.
