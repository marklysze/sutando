#!/usr/bin/env bash
# tmux-pane-keys.sh [--tmux BIN] -S SOCKET -t TARGET [--timeout SECS] -- <send-keys args...>
# The one way to send keys into an agent pane: leave any pane mode first, then send-keys
# under a timeout. Exit: send-keys' own status · 124 timed out · 2 usage.
#
# In copy mode every typed letter runs a copy-mode binding, and f/F/t/T/g open a
# command-prompt that blocks the sending client until someone answers it.
set -u
TMUX_BIN=tmux; SOCK=""; TARGET=""; TIMEOUT=5
while [ $# -gt 0 ]; do
  case "$1" in
    --tmux) TMUX_BIN="${2:?}"; shift 2 ;;
    -S) SOCK="${2:?}"; shift 2 ;;
    -t) TARGET="${2:?}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?}"; shift 2 ;;
    --) shift; break ;;
    *) echo "tmux-pane-keys: unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$SOCK" ] && [ -n "$TARGET" ] && [ $# -gt 0 ] || { echo "tmux-pane-keys: need -S, -t and keys after --" >&2; exit 2; }
case "$TIMEOUT" in ''|*[!0-9]*|0) echo "tmux-pane-keys: --timeout takes whole seconds" >&2; exit 2 ;; esac

# Runs one tmux command, killing it after TIMEOUT seconds (macOS ships no timeout(1)).
bounded() {
  "$TMUX_BIN" -S "$SOCK" "$@" & local pid=$!
  local ticks=$(( TIMEOUT * 50 ))
  while [ "$ticks" -gt 0 ] && kill -0 "$pid" 2>/dev/null; do
    sleep 0.02; ticks=$((ticks - 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    echo "tmux-pane-keys: $1 to $TARGET timed out after ${TIMEOUT}s" >&2
    return 124
  fi
  wait "$pid"
}

if [ "$("$TMUX_BIN" -S "$SOCK" display-message -p -t "$TARGET" '#{pane_in_mode}' 2>/dev/null)" = 1 ]; then
  bounded copy-mode -q -t "$TARGET" || exit $?
fi
bounded send-keys -t "$TARGET" "$@"
