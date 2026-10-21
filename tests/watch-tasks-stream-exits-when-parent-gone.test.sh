#!/usr/bin/env bash
# A session watcher whose starting parent is SIGKILLed (a crashed core runs no Stop
# hook) exits on its own within seconds and releases its sentinel; while the parent
# lives it stays up, and a standby is not subject to the check.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-parentgone.XXXXXX")"
mkdir -p "$WORK/stubbin"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$WORK/stubbin/fswatch"; chmod +x "$WORK/stubbin/fswatch"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM -- "-$p" 2>/dev/null; [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; done
  sleep 0.5
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -KILL -- "-$p" 2>/dev/null; [ -n "$p" ] && kill -KILL "$p" 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT
check() { if [ "$2" = 0 ]; then echo "  PASS $1"; else echo "  FAIL $1${3:+ — $3}"; fail=1; fi; }
alive() { kill -0 "$1" 2>/dev/null; }
# start <ws> <role>: a parent shell in its own session starts the watcher and waits
# on it, as a Monitor does; prints the parent's pid. The watcher's pid lands in <ws>/w.pid.
start() {
  local ws="$1" role="$2"
  env -u SUTANDO_INSTANCE_ID -u SUTANDO_AGENT_ID -u AGENT_ID -u AGENT_MXID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE -u SUTANDO_CORE_SESSION \
      SUTANDO_WORKSPACE_DIR="$ws" PATH="$WORK/stubbin:$PATH" W="$WATCHER" WS="$ws" ROLE="$role" \
      python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash -c 'bash "$W" "$WS/tasks" --role "$ROLE" --inbox "$WS/tasks" > "$WS/w.out" 2> "$WS/w.err" & echo $! > "$WS/w.pid"; wait' > /dev/null 2>&1 &
  echo $!
}
stamped() {  # stamped <ws>: wait for the watcher to stamp its sentinel; prints its pid
  local ws="$1" i w
  for i in $(seq 1 100); do
    w="$(cat "$ws/w.pid" 2>/dev/null)"
    [ -n "$w" ] && [ "$(cat "$ws"/state/*.pid 2>/dev/null | head -1)" = "$w" ] && { echo "$w"; return 0; }
    sleep 0.1
  done
  cat "$ws/w.pid" 2>/dev/null
  return 1
}
gone_within() {  # gone_within <pid> <seconds>
  local i
  for i in $(seq 1 $(( $2 * 10 ))); do alive "$1" || return 0; sleep 0.1; done
  return 1
}

echo "watch-tasks-stream exits when its parent is gone:"

# (a) Parent SIGKILLed: the session watcher exits, releases its sentinel, and takes fswatch with it.
WS="$WORK/killed"; mkdir -p "$WS/tasks" "$WS/state"
parent="$(start "$WS" session)"; PIDS+=("$parent")
w="$(stamped "$WS")"; check "(a) the session watcher started and stamped" $? "$(tail -2 "$WS/w.err" | tr '\n' '|')"
fsw="$(pgrep -P "$w" -f fswatch | head -1)"
kill -KILL "$parent"
gone_within "$w" 10; check "(a) after kill -9 of its parent the watcher exits within 10s" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
grep -q 'the process that started this watcher (pid [0-9]*) is gone; exiting' "$WS/w.err"; check "(a) ...and says why on stderr" $?
[ -z "$(ls "$WS"/state/*.pid 2>/dev/null)" ]; check "(a) ...and releases its sentinel" $?
{ [ -z "$fsw" ] || gone_within "$fsw" 3; }; check "(a) ...and its fswatch is gone" $?

# (b) Parent alive: the session watcher stays up past several checks.
WS="$WORK/alive"; mkdir -p "$WS/tasks" "$WS/state"
parent="$(start "$WS" session)"; PIDS+=("$parent")
w="$(stamped "$WS")"
sleep 8
alive "$w" && [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$w" ]; check "(b) with its parent alive the session watcher is still up after 8s" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
kill -TERM -- "-$parent" 2>/dev/null; sleep 0.3

# (c) A standby is not subject to the check: its parent dying leaves it running.
WS="$WORK/standby"; mkdir -p "$WS/tasks" "$WS/state"
parent="$(start "$WS" standby)"; PIDS+=("$parent")
w="$(stamped "$WS")"
kill -KILL "$parent"
sleep 8
alive "$w"; check "(c) a standby whose parent is SIGKILLed keeps running" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
kill -TERM -- "-$parent" 2>/dev/null; kill -TERM "$w" 2>/dev/null; sleep 0.3

echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi
