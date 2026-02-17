#!/usr/bin/env bash
set -euo pipefail

# NSE market hours in IST (configurable via env vars).
MARKET_TZ="${MARKET_TZ:-Asia/Kolkata}"
MARKET_OPEN_HHMM="${MARKET_OPEN_HHMM:-09:15}"
MARKET_CLOSE_HHMM="${MARKET_CLOSE_HHMM:-15:30}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-5}"

child_pid=""
last_state=""

log() {
  printf '[%s] %s\n' "$(TZ="$MARKET_TZ" date '+%Y-%m-%d %H:%M:%S %Z')" "$*"
}

market_state() {
  python3 - "$MARKET_TZ" "$MARKET_OPEN_HHMM" "$MARKET_CLOSE_HHMM" <<'PY'
import math
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

tz_name, open_hhmm, close_hhmm = sys.argv[1], sys.argv[2], sys.argv[3]
tz = ZoneInfo(tz_name)
open_hour, open_minute = map(int, open_hhmm.split(":"))
close_hour, close_minute = map(int, close_hhmm.split(":"))

now = datetime.now(tz=tz)

def dt_on(day, hour, minute):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)

def next_weekday_open(start_day):
    day = start_day
    while day.weekday() >= 5:  # Saturday/Sunday
        day += timedelta(days=1)
    return dt_on(day, open_hour, open_minute)

today_open = dt_on(now.date(), open_hour, open_minute)
today_close = dt_on(now.date(), close_hour, close_minute)

if now.weekday() >= 5:
    next_open = next_weekday_open(now.date() + timedelta(days=1))
    wait_seconds = max(1, math.ceil((next_open - now).total_seconds()))
    print(f"WAIT|weekend|{wait_seconds}|{next_open.isoformat()}")
elif now < today_open:
    wait_seconds = max(1, math.ceil((today_open - now).total_seconds()))
    print(f"WAIT|before_open|{wait_seconds}|{today_open.isoformat()}")
elif now >= today_close:
    next_open = next_weekday_open(now.date() + timedelta(days=1))
    wait_seconds = max(1, math.ceil((next_open - now).total_seconds()))
    print(f"WAIT|after_close|{wait_seconds}|{next_open.isoformat()}")
else:
    seconds_to_close = max(1, math.ceil((today_close - now).total_seconds()))
    print(f"IN_MARKET|trading_session|{seconds_to_close}|{today_close.isoformat()}")
PY
}

start_stream() {
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    return
  fi

  log "Starting stream process: python -m engines.zerodha.run_option_chain $*"
  python -m engines.zerodha.run_option_chain "$@" &
  child_pid="$!"
  log "Stream PID: $child_pid"
}

stop_stream() {
  local reason="${1:-outside_market_hours}"
  if [[ -z "$child_pid" ]] || ! kill -0 "$child_pid" 2>/dev/null; then
    child_pid=""
    return
  fi

  log "Stopping stream process ($reason), PID $child_pid"
  kill -INT "$child_pid" 2>/dev/null || true

  for _ in {1..20}; do
    if ! kill -0 "$child_pid" 2>/dev/null; then
      child_pid=""
      log "Stream stopped gracefully"
      return
    fi
    sleep 1
  done

  log "Graceful stop timeout reached; sending SIGTERM"
  kill -TERM "$child_pid" 2>/dev/null || true

  for _ in {1..10}; do
    if ! kill -0 "$child_pid" 2>/dev/null; then
      child_pid=""
      log "Stream terminated"
      return
    fi
    sleep 1
  done

  log "Force killing stream PID $child_pid"
  kill -KILL "$child_pid" 2>/dev/null || true
  child_pid=""
}

cleanup() {
  stop_stream "scheduler_shutdown"
  exit 0
}

trap cleanup INT TERM

log "Scheduler started (TZ=$MARKET_TZ, open=$MARKET_OPEN_HHMM, close=$MARKET_CLOSE_HHMM)"

while true; do
  IFS='|' read -r state reason seconds target_time <<<"$(market_state)"

  if [[ "$state" != "$last_state" ]]; then
    if [[ "$state" == "IN_MARKET" ]]; then
      log "Market is open; target close time: $target_time"
    else
      log "Market is closed ($reason); next open/target time: $target_time"
    fi
    last_state="$state"
  fi

  if [[ "$state" == "IN_MARKET" ]]; then
    start_stream "$@"

    if [[ -n "$child_pid" ]] && ! kill -0 "$child_pid" 2>/dev/null; then
      wait "$child_pid" || true
      child_pid=""
      log "Stream exited during market hours; retrying in 5 seconds"
      sleep 5
      continue
    fi

    sleep_for="$CHECK_INTERVAL_SECONDS"
    if (( seconds < sleep_for )); then
      sleep_for="$seconds"
    fi
    (( sleep_for < 1 )) && sleep_for=1
    sleep "$sleep_for"
  else
    stop_stream "outside_market_hours"
    sleep_for="$seconds"
    if (( sleep_for > 300 )); then
      sleep_for=300
    fi
    (( sleep_for < 1 )) && sleep_for=1
    sleep "$sleep_for"
  fi
done
