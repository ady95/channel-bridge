#!/usr/bin/env bash
# 개발용 브릿지 프로세스 관리.
#
#   ./bridge.sh start     기동 (WebSocket 연결 확립까지 대기)
#   ./bridge.sh stop      정상 종료 (SIGTERM)
#   ./bridge.sh kill      강제 종료 (SIGKILL - 크래시 복구 테스트용)
#   ./bridge.sh restart
#   ./bridge.sh status
#   ./bridge.sh log [줄수]
#
# SSH 세션에서 호출해도 즉시 반환하도록 setsid 로 완전히 분리한다.

set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

LOG=/tmp/chbridge.log
PIDFILE=/tmp/chbridge.pid
PATTERN='chbridge(\.cli)? run|python -m chbridge'

pids() { pgrep -f 'chbridge' 2>/dev/null | while read -r p; do
    cmd=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)
    case "$cmd" in *"chbridge run"*|*"-m chbridge"*) echo "$p" ;; esac
  done; }

cmd_status() {
  local found
  found=$(pids | tr '\n' ' ')
  if [ -n "${found// /}" ]; then
    echo "실행 중: PID $found"
    return 0
  fi
  echo "실행 중이 아님"
  return 1
}

cmd_start() {
  if cmd_status >/dev/null; then
    echo "이미 실행 중입니다."
    cmd_status
    return 0
  fi
  : > "$LOG"
  # setsid: 새 세션으로 분리해 SSH 종료에 영향받지 않는다
  # </dev/null, >log 2>&1: SSH 가 fd 를 붙잡지 않도록 전부 리다이렉션
  setsid uv run python -m chbridge run < /dev/null > "$LOG" 2>&1 &
  disown 2>/dev/null || true

  # 양쪽 WebSocket 이 붙을 때까지 기다린다
  local n=0
  for _ in $(seq 1 80); do
    n=$(grep -c 'mm.ws_ready' "$LOG" 2>/dev/null)
    [ -z "$n" ] && n=0
    if [ "$n" -ge 2 ]; then
      echo "기동 완료 (ws_ready ${n}건)"
      cmd_status
      return 0
    fi
    if grep -qE 'Traceback|job_failed' "$LOG" 2>/dev/null; then
      echo "기동 중 오류:"
      tail -25 "$LOG"
      return 1
    fi
    sleep 0.5
  done
  echo "기동 대기 시간 초과 (ws_ready ${n}건)"
  tail -25 "$LOG"
  return 1
}

cmd_stop() {
  local found
  found=$(pids | tr '\n' ' ')
  if [ -z "${found// /}" ]; then echo "실행 중이 아님"; return 0; fi
  # shellcheck disable=SC2086
  kill -TERM $found 2>/dev/null
  for _ in $(seq 1 20); do
    cmd_status >/dev/null || { echo "정상 종료됨"; return 0; }
    sleep 0.3
  done
  echo "SIGTERM 무응답 - 강제 종료"
  cmd_kill
}

cmd_kill() {
  local found
  found=$(pids | tr '\n' ' ')
  if [ -z "${found// /}" ]; then echo "실행 중이 아님"; return 0; fi
  # shellcheck disable=SC2086
  kill -9 $found 2>/dev/null
  sleep 1
  echo "강제 종료됨 (PID $found)"
}

case "${1:-status}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  kill)    cmd_kill ;;
  restart) cmd_stop; sleep 1; cmd_start ;;
  status)  cmd_status ;;
  log)     tail -n "${2:-40}" "$LOG" ;;
  *)       echo "사용법: $0 {start|stop|kill|restart|status|log [줄수]}"; exit 1 ;;
esac
