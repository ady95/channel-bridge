#!/usr/bin/env bash
# Channel Bridge - 개발/테스트 픽스처 생성
#
# mm-a / mm-b 양쪽에 팀·채널·사용자·브릿지 Bot을 만든다.
# LocalMode(인증 불필요)를 쓰므로 서버가 기동된 상태에서 실행하면 된다.
#
#   ./fixtures.sh              전체 생성
#   ./fixtures.sh --show       생성 결과만 조회
#
# 재실행해도 안전하다 (이미 존재하는 항목은 건너뛴다).
# Bot 토큰은 .tokens.env 에 기록되며 git에서 제외된다.

set -uo pipefail
cd "$(dirname "$0")"

PASSWORD='Bridge-Test-1234'
TEAM='bridge'
TEAM_DISPLAY='Bridge Test'
CHANNELS=('bridge-1:Bridge 1' 'bridge-2:Bridge 2')
BOT='bridgebot'
TOKENS_FILE='.tokens.env'

# 서버별 사용자 - 일부러 다르게 둔다.
# MM<->MM 전달 시 "반대편에 존재하지 않는 사용자"의 표시가 올바른지 검증하기 위함.
USERS_A=('alice:Alice Kim' 'bob:Bob Lee')
USERS_B=('carol:Carol Park' 'dave:Dave Choi')

mm() { local svc="$1"; shift; docker compose exec -T "$svc" mmctl --local "$@" 2>&1; }

# 서비스 -> 호스트 포트
port_of() { case "$1" in mm-a) echo 8071 ;; mm-b) echo 8072 ;; *) return 1 ;; esac; }

jget() { python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print(d.get('$1','') if isinstance(d,dict) else '')
" 2>/dev/null; }

# Bot 생성은 LocalMode에서 금지되어 있어(mmctl: 'cannot be run in local mode')
# 관리자로 로그인해 REST API로 처리한다.
create_bot() {
  local svc="$1" base admin_token bot_id bot_token
  base="http://localhost:$(port_of "$svc")/api/v4"

  admin_token="$(curl -sS -D - -o /dev/null -X POST "$base/users/login" \
      -H 'Content-Type: application/json' \
      -d "{\"login_id\":\"admin@test.local\",\"password\":\"$PASSWORD\"}" \
      | awk 'tolower($1)=="token:"{print $2}' | tr -d '\r')"
  if [[ -z "$admin_token" ]]; then
    echo "  [실패] bot: 관리자 로그인 실패"; return 1
  fi

  bot_id="$(curl -sS -X POST "$base/bots" \
      -H "Authorization: Bearer $admin_token" -H 'Content-Type: application/json' \
      -d "{\"username\":\"$BOT\",\"display_name\":\"Channel Bridge\",\"description\":\"채널 브릿지 전용 봇\"}" \
      | jget user_id)"

  if [[ -n "$bot_id" ]]; then
    echo "  [생성] bot: $BOT"
  else
    # 이미 존재하면 사용자 조회로 id를 얻는다
    bot_id="$(curl -sS "$base/users/username/$BOT" \
        -H "Authorization: Bearer $admin_token" | jget id)"
    [[ -n "$bot_id" ]] && echo "  [존재] bot: $BOT (토큰 재발급)" \
                       || { echo "  [실패] bot: $BOT 생성/조회 모두 실패"; return 1; }
  fi

  bot_token="$(curl -sS -X POST "$base/users/$bot_id/tokens" \
      -H "Authorization: Bearer $admin_token" -H 'Content-Type: application/json' \
      -d '{"description":"bridge relay token"}' | jget token)"

  if [[ -n "$bot_token" ]]; then
    {
      printf '%s_BOT_ID=%s\n'    "$(tr 'a-z-' 'A-Z_' <<<"$svc")" "$bot_id"
      printf '%s_BOT_TOKEN=%s\n' "$(tr 'a-z-' 'A-Z_' <<<"$svc")" "$bot_token"
    } >> "$TOKENS_FILE"
    echo "  [토큰] $BOT -> $TOKENS_FILE 기록 (${#bot_token}자)"
  else
    echo "  [경고] $BOT 토큰 발급 실패"
  fi

  curl -sS -X DELETE "$base/users/logout" -H "Authorization: Bearer $admin_token" >/dev/null
}

# 실패를 허용하는 실행. 이미 존재하는 리소스는 정상으로 취급한다.
try() {
  local label="$1"; shift
  local out; out="$("$@" 2>&1)"
  if [[ $? -eq 0 ]]; then
    printf '  [생성] %s\n' "$label"
  elif grep -qiE 'already exists|already a member|existing|unable to create.*exist' <<<"$out"; then
    printf '  [존재] %s\n' "$label"
  else
    printf '  [실패] %s\n         %s\n' "$label" "$(head -2 <<<"$out" | tr '\n' ' ')"
  fi
}

seed_server() {
  local svc="$1"; shift
  local -n users_ref="$1"
  echo "=============================================="
  echo " $svc"
  echo "=============================================="

  # --- 관리자 ---
  try "admin@test.local (system-admin)" \
    mm "$svc" user create --email admin@test.local --username admin \
       --password "$PASSWORD" --system-admin --email-verified --disable-welcome-email

  # --- 팀 ---
  try "team: $TEAM" \
    mm "$svc" team create --name "$TEAM" --display-name "$TEAM_DISPLAY" --email admin@test.local
  try "team member: admin" mm "$svc" team users add "$TEAM" admin@test.local

  # --- 일반 사용자 ---
  local entry uname full
  for entry in "${users_ref[@]}"; do
    uname="${entry%%:*}"; full="${entry#*:}"
    try "$uname@test.local ($full)" \
      mm "$svc" user create --email "$uname@test.local" --username "$uname" \
         --password "$PASSWORD" --email-verified --disable-welcome-email \
         --firstname "${full%% *}" --lastname "${full#* }"
    try "team member: $uname" mm "$svc" team users add "$TEAM" "$uname@test.local"
  done

  # --- 브릿지 Bot ---
  create_bot "$svc"
  try "team member: $BOT" mm "$svc" team users add "$TEAM" "$BOT"

  # --- 채널 + 멤버 ---
  local ch cname cdisp
  for ch in "${CHANNELS[@]}"; do
    cname="${ch%%:*}"; cdisp="${ch#*:}"
    try "channel: $cname" \
      mm "$svc" channel create --team "$TEAM" --name "$cname" --display-name "$cdisp"
    try "channel member: admin, $BOT" \
      mm "$svc" channel users add "${TEAM}:${cname}" admin@test.local "$BOT"
    for entry in "${users_ref[@]}"; do
      uname="${entry%%:*}"
      try "channel member: $uname" \
        mm "$svc" channel users add "${TEAM}:${cname}" "$uname@test.local"
    done
  done
  echo
}

show() {
  local svc
  for svc in mm-a mm-b; do
    echo "===== $svc ====="
    echo "-- 사용자 --"; mm "$svc" user list --per-page 50 | sed 's/^/  /'
    echo "-- 팀 --";     mm "$svc" team list             | sed 's/^/  /'
    echo "-- 채널 --";   mm "$svc" channel list "$TEAM"  | sed 's/^/  /'
    echo "-- 봇 --";     mm "$svc" bot list              | sed 's/^/  /'
    echo
  done
}

if [[ "${1:-}" == "--show" ]]; then
  show
  exit 0
fi

: > "$TOKENS_FILE"
{
  echo "# 자동 생성 - 커밋 금지"
  echo "# Mattermost 브릿지 Bot 액세스 토큰"
} >> "$TOKENS_FILE"
chmod 600 "$TOKENS_FILE"

seed_server mm-a USERS_A
seed_server mm-b USERS_B

echo "=============================================="
echo " 완료"
echo "=============================================="
# 두 서버를 같은 호스트의 다른 포트로 열면 쿠키가 서로를 덮어써서 한쪽이
# 로그아웃된다. compose 가 SiteURL 을 호스트명으로 나눠 두었으므로 브라우저는
# 반드시 아래 주소로 접속해야 한다. (docker-compose.yml 상단 주석 참고)
echo "  MM A : $(docker compose config 2>/dev/null | awk -F'SITEURL: ' '/SITEURL/{print $2}' | sed -n 1p)"
echo "  MM B : $(docker compose config 2>/dev/null | awk -F'SITEURL: ' '/SITEURL/{print $2}' | sed -n 2p)"
echo "  계정 : admin / alice / bob (A), admin / carol / dave (B)"
echo "  비밀번호: $PASSWORD"
echo "  Bot 토큰: $(pwd)/$TOKENS_FILE (chmod 600)"
