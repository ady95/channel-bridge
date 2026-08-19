#!/usr/bin/env bash
# Channel Bridge - 개발 환경 상태 검증
#
#   ./verify.sh
#
# 컨테이너 상태, Mattermost 에디션, 브릿지 필수 설정, Bot 토큰 인증까지
# 한 번에 확인한다. Phase 0 착수 전 / 환경 재구성 후에 실행한다.
# 비밀값은 출력하지 않는다.

set -uo pipefail
cd "$(dirname "$0")"

FAIL=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

jval() { grep -o "\"$1\":\"[^\"]*\"" "$2" 2>/dev/null | head -1 | sed "s/.*:\"//;s/\"$//"; }
jbool() { grep -o "\"$1\":[a-z]*" "$2" 2>/dev/null | head -1 | sed 's/.*://'; }

# ---------------------------------------------------------------- 컨테이너
head_ "1. 컨테이너"
for svc in mm-a mm-b db-a db-b bridge-db; do
  st="$(docker compose ps --format '{{.Service}} {{.Status}}' 2>/dev/null | awk -v s="$svc" '$1==s{$1="";print}')"
  case "$st" in
    *healthy*) ok "$svc:$st" ;;
    "")        bad "$svc: 미기동" ;;
    *)         warn "$svc:$st" ;;
  esac
done

# ---------------------------------------------------------------- 에디션
head_ "2. Mattermost 에디션 (Team Edition 이어야 함)"
for pair in "mm-a 8071" "mm-b 8072"; do
  set -- $pair
  svc="$1"; port="$2"
  tmp="$(mktemp)"
  if ! curl -sS --max-time 10 "http://localhost:$port/api/v4/config/client?format=old" -o "$tmp"; then
    bad "$svc: 응답 없음"; rm -f "$tmp"; continue
  fi
  ent="$(jval BuildEnterpriseReady "$tmp")"
  ver="$(jval Version "$tmp")"
  if [ "$ent" = "false" ]; then
    ok "$svc: Team Edition (v$ver, BuildEnterpriseReady=false)"
  else
    bad "$svc: Enterprise 바이너리 (BuildEnterpriseReady=$ent) - Entry 모드 위험"
  fi
  rm -f "$tmp"
done

# ---------------------------------------------------------------- 필수 설정
head_ "3. 브릿지 필수 설정 (PRD 4.5)"
declare -A EXPECT=(
  [ServiceSettings.EnablePostUsernameOverride]=true
  [ServiceSettings.EnablePostIconOverride]=true
  [ServiceSettings.EnableUserAccessTokens]=true
  [ServiceSettings.EnableBotAccountCreation]=true
  [ServiceSettings.EnableIncomingWebhooks]=true
  [ServiceSettings.EnableLocalMode]=true
  [PluginSettings.Enable]=true
)
for svc in mm-a mm-b; do
  miss=""
  for k in "${!EXPECT[@]}"; do
    got="$(docker compose exec -T "$svc" mmctl --local config get "$k" 2>/dev/null | tr -d ' \r\n')"
    [ "$got" = "${EXPECT[$k]}" ] || miss="$miss $k(=$got)"
  done
  su="$(docker compose exec -T "$svc" mmctl --local config get ServiceSettings.SiteURL 2>/dev/null | tr -d ' \r\n"')"
  [ -n "$su" ] || miss="$miss SiteURL(빈값)"
  if [ -z "$miss" ]; then ok "$svc: 전체 정상 (SiteURL=$su)"; else bad "$svc: 불일치$miss"; fi
done

# ---------------------------------------------------------------- Bot 토큰
head_ "4. 브릿지 Bot 토큰 인증"
if [ ! -f .tokens.env ]; then
  bad ".tokens.env 없음 - ./fixtures.sh 를 먼저 실행"
else
  # shellcheck disable=SC1091
  set -a; . ./.tokens.env; set +a
  for pair in "mm-a 8071 MM_A_BOT_TOKEN" "mm-b 8072 MM_B_BOT_TOKEN"; do
    set -- $pair
    svc="$1"; port="$2"; var="$3"
    tok="${!var:-}"
    if [ -z "$tok" ]; then bad "$svc: $var 없음"; continue; fi
    tmp="$(mktemp)"
    code="$(curl -sS --max-time 10 -o "$tmp" -w '%{http_code}' \
            "http://localhost:$port/api/v4/users/me" -H "Authorization: Bearer $tok")"
    if [ "$code" = "200" ]; then
      ok "$svc: 인증 성공 (username=$(jval username "$tmp"), is_bot=$(jbool is_bot "$tmp"))"
    else
      bad "$svc: HTTP $code"
    fi
    rm -f "$tmp"
  done
fi

# ---------------------------------------------------------------- 픽스처
head_ "5. 픽스처 (팀/채널/사용자)"
for svc in mm-a mm-b; do
  users="$(docker compose exec -T "$svc" mmctl --local user list --per-page 50 2>/dev/null | grep -c . )"
  chans="$(docker compose exec -T "$svc" mmctl --local channel list bridge 2>/dev/null | grep -cE 'bridge-[0-9]')"
  if [ "${users:-0}" -ge 4 ] && [ "${chans:-0}" -ge 2 ]; then
    ok "$svc: 사용자 ${users}명, bridge-* 채널 ${chans}개"
  else
    bad "$svc: 사용자 ${users:-0}명, bridge-* 채널 ${chans:-0}개 - ./fixtures.sh 실행 필요"
  fi
done

# ---------------------------------------------------------------- 브릿지 DB
head_ "6. 브릿지 저장소"
v="$(docker compose exec -T bridge-db psql -U bridge -d bridge -t -A -c 'select version();' 2>/dev/null | head -1)"
[ -n "$v" ] && ok "bridge-db: ${v%% on *}" || bad "bridge-db: 연결 실패"

# ---------------------------------------------------------------- 비밀 관리
head_ "7. 비밀 파일 관리"
for f in .env .tokens.env; do
  [ -f "$f" ] || { warn "$f 없음"; continue; }
  perm="$(stat -c %a "$f")"
  [ "$perm" = "600" ] && ok "$f 권한 $perm" || warn "$f 권한 $perm (600 권장)"
  if git -C .. check-ignore -q "dev/$f" 2>/dev/null; then ok "$f gitignore 처리됨"
  else bad "$f 가 git에 노출됨"; fi
done

head_ "결과"
if [ "$FAIL" -eq 0 ]; then
  printf '  \033[32m전체 통과\033[0m\n'
else
  printf '  \033[31m실패 %d건\033[0m\n' "$FAIL"
fi
exit $((FAIL > 0))
