#!/usr/bin/env bash
# Phase 1 완료 조건: "서비스 강제 종료 후 재시작 시 유실 0건" (NFR-2 / FR-8)
#
# 브릿지를 SIGKILL 로 죽이고, 죽은 동안 메시지를 넣은 뒤 재시작해서
# sync_cursor 백필이 누락 없이 복구하는지 확인한다.
#
# WebSocket 은 끊긴 동안의 이벤트를 재생해주지 않으므로, 백필이 없으면
# 이 구간의 메시지는 **조용히** 사라진다. 로그에도 남지 않아 가장 위험하다.

set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

TAG="CRASH-$(( RANDOM % 100000 ))"
LOG=/tmp/chbridge.log
PIDFILE=/tmp/chbridge.pid

PASS=$'\033[32m✓\033[0m'
FAIL=$'\033[31m✗\033[0m'
fails=0
ck() { if [ "$1" = "0" ]; then printf '  %s %s\n' "$PASS" "$2"; else printf '  %s %s\n' "$FAIL" "$2"; fails=$((fails+1)); fi; }

start_bridge() {
  nohup uv run python -m chbridge run >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 60); do
    if [ "$(grep -c 'mm.ws_ready' "$LOG")" -ge 2 ]; then return 0; fi
    sleep 0.5
  done
  return 1
}

stop_bridge_hard() {
  if [ -f "$PIDFILE" ]; then
    # 프로세스 그룹 전체를 SIGKILL. graceful shutdown 경로를 타지 않게 한다.
    kill -9 "$(cat "$PIDFILE")" 2>/dev/null
    pkill -9 -f 'chbridge run' 2>/dev/null
    rm -f "$PIDFILE"
  fi
  sleep 1.5
}

echo "==================================================================="
echo " 크래시 복구 검증  태그=$TAG"
echo "==================================================================="

# ---------------------------------------------------------------- 준비
eval "$(grep -E '^(HOST_ADDR|MM_A_PORT|MM_B_PORT)=' dev/.env)"
A="http://${HOST_ADDR}:${MM_A_PORT}/api/v4"
B="http://${HOST_ADDR}:${MM_B_PORT}/api/v4"
PW='Bridge-Test-1234'

login() { curl -sS -D - -o /dev/null -X POST "$1/users/login" \
  -H 'Content-Type: application/json' \
  -d "{\"login_id\":\"$2\",\"password\":\"$PW\"}" | awk 'tolower($1)=="token:"{print $2}' | tr -d '\r'; }

A_ADMIN=$(login "$A" admin@test.local)
A_ALICE=$(login "$A" alice@test.local)
B_ADMIN=$(login "$B" admin@test.local)

chan() { curl -sS "$1/teams/name/bridge/channels/name/bridge-1" -H "Authorization: Bearer $2" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'; }
A_CH=$(chan "$A" "$A_ADMIN")
B_CH=$(chan "$B" "$B_ADMIN")

count_in_b() { curl -sS "$B/channels/$B_CH/posts?per_page=100" -H "Authorization: Bearer $B_ADMIN" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
tag=sys.argv[1]
print(sum(1 for p in d['posts'].values() if tag in p.get('message','') and not p.get('delete_at')))
" "$1"; }

# 브릿지가 살아 있어야 커서가 만들어져 있다
if ! pgrep -f 'chbridge run' >/dev/null; then
  echo "  브릿지를 기동합니다..."
  start_bridge || { echo "기동 실패"; exit 1; }
fi

# ---------------------------------------------------------------- 1) 커서 확보
echo
echo "1. 크래시 전 메시지로 커서를 전진시킨다"
curl -sS -o /dev/null -X POST "$A/posts" -H "Authorization: Bearer $A_ALICE" \
  -H 'Content-Type: application/json' \
  -d "{\"channel_id\":\"$A_CH\",\"message\":\"$TAG 크래시전\"}"
sleep 5
n=$(count_in_b "$TAG 크래시전")
ck "$([ "$n" = "1" ] && echo 0 || echo 1)" "크래시 전 메시지 전달됨 (B에 ${n}건)"

CURSOR_BEFORE=$(uv run python -m chbridge status 2>/dev/null | grep -A3 '백필 커서' | grep -oE '[0-9]{10,}' | head -1)
echo "      커서: ${CURSOR_BEFORE:-(없음)}"
ck "$([ -n "${CURSOR_BEFORE:-}" ] && echo 0 || echo 1)" "sync_cursor 가 기록되어 있음"

# ---------------------------------------------------------------- 2) 강제 종료
echo
echo "2. SIGKILL 로 강제 종료 (graceful shutdown 경로를 타지 않음)"
stop_bridge_hard
if pgrep -f 'chbridge run' >/dev/null; then ck 1 "프로세스 종료"; else ck 0 "프로세스 종료 확인"; fi

# ---------------------------------------------------------------- 3) 죽은 동안 메시지 투입
echo
echo "3. 브릿지가 죽은 동안 메시지 3건 투입"
for i in 1 2 3; do
  curl -sS -o /dev/null -X POST "$A/posts" -H "Authorization: Bearer $A_ALICE" \
    -H 'Content-Type: application/json' \
    -d "{\"channel_id\":\"$A_CH\",\"message\":\"$TAG 단절중-$i\"}"
  sleep 0.4
done
missed=0
for i in 1 2 3; do
  n=$(count_in_b "$TAG 단절중-$i")
  [ "$n" = "0" ] || missed=$((missed+1))
done
ck "$([ "$missed" = "0" ] && echo 0 || echo 1)" "이 시점에는 B 에 전달되지 않음 (당연)"

# ---------------------------------------------------------------- 4) 재시작
echo
echo "4. 재시작 -> 백필이 단절 구간을 복구해야 한다"
start_bridge || { echo "재기동 실패"; tail -20 "$LOG"; exit 1; }

recovered=0
for _ in $(seq 1 40); do
  recovered=0
  for i in 1 2 3; do
    n=$(count_in_b "$TAG 단절중-$i")
    [ "$n" = "1" ] && recovered=$((recovered+1))
  done
  [ "$recovered" = "3" ] && break
  sleep 1
done

echo "      복구된 메시지: ${recovered}/3"
ck "$([ "$recovered" = "3" ] && echo 0 || echo 1)" "단절 구간 3건 전부 복구 (FR-8 백필)"

# ---------------------------------------------------------------- 5) 중복 없음
echo
echo "5. 백필-스트림 중복 구간 검증"
sleep 6
dup=0
for i in 1 2 3; do
  n=$(count_in_b "$TAG 단절중-$i")
  [ "$n" = "1" ] || { dup=$((dup+1)); echo "      단절중-$i 가 ${n}건 (중복!)"; }
done
n=$(count_in_b "$TAG 크래시전")
[ "$n" = "1" ] || { dup=$((dup+1)); echo "      크래시전 이 ${n}건 (중복!)"; }
ck "$([ "$dup" = "0" ] && echo 0 || echo 1)" "각 메시지가 정확히 1건 (event_inbox UNIQUE 가 중복 차단)"

# ---------------------------------------------------------------- 결과
echo
echo "==================================================================="
if [ "$fails" = "0" ]; then
  printf ' \033[32m전체 통과 - NFR-2 유실 0건 / 중복 0건\033[0m\n'
else
  printf ' \033[31m실패 %d건\033[0m\n' "$fails"
  echo
  echo "최근 브릿지 로그:"
  tail -30 "$LOG"
fi
echo "==================================================================="
exit $(( fails > 0 ))
