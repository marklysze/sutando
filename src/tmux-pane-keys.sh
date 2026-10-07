#!/usr/bin/env bash
# tmux-pane-keys.sh [--tmux BIN] -S SOCKET -t TARGET [--timeout SECS] -- <send-keys args...>
# Leave pane mode before sending; each tmux operation is bounded. Exit: command status, 124 timeout, 2 usage.

# Copy-mode letters can open a command-prompt that blocks the sending client.
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
case "$TIMEOUT" in ''|*[!0-9]*) echo "tmux-pane-keys: --timeout takes whole seconds" >&2; exit 2 ;; esac

[ "$((10#$TIMEOUT))" -gt 0 ] || { echo "tmux-pane-keys: --timeout must be positive" >&2; exit 2; }
source "$(dirname "${BASH_SOURCE[0]}")/bounded-wait.sh"
TIMEOUT_FLAG="$(mktemp "${TMPDIR:-/tmp}/tmux-pane-keys.XXXXXX")" || exit 1
trap 'rm -f "$TIMEOUT_FLAG"' EXIT

bounded() {
  local rc
  rm -f "$TIMEOUT_FLAG"
  run_bounded "$TIMEOUT" "$TIMEOUT_FLAG" -- "$TMUX_BIN" -S "$SOCK" "$@"
  rc=$?
  if [ -e "$TIMEOUT_FLAG" ]; then
    echo "tmux-pane-keys: $1 to $TARGET timed out after ${TIMEOUT}s" >&2
    return 124
  fi
  return "$rc"
}

if [ "$(bounded display-message -p -t "$TARGET" '#{pane_in_mode}' 2>/dev/null)" = 1 ]; then
  bounded copy-mode -q -t "$TARGET" || exit $?
fi
bounded send-keys -t "$TARGET" "$@"
