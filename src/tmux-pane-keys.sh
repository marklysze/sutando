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
WORK="$(mktemp -d "${TMPDIR:-/tmp}/tmux-pane-keys.XXXXXX")" || exit 1
trap 'rm -rf "$WORK"' EXIT
TIMEOUT_FLAG="$WORK/timed-out"

# Output goes to files, never to a pipe anyone waits on: a client killed while its server is
# stopped leaves its fds with that server, which would hold a pipe open past the bound.
bounded() {
  local rc
  rm -f "$TIMEOUT_FLAG"
  run_bounded "$TIMEOUT" "$TIMEOUT_FLAG" -- "$TMUX_BIN" -S "$SOCK" "$@" > "$WORK/out" 2> "$WORK/err" < /dev/null
  rc=$?
  if [ -e "$TIMEOUT_FLAG" ]; then
    echo "tmux-pane-keys: $1 to $TARGET timed out after ${TIMEOUT}s" >&2
    return 124
  fi
  return "$rc"
}

if bounded display-message -p -t "$TARGET" '#{pane_in_mode}' && [ "$(cat "$WORK/out")" = 1 ]; then
  bounded copy-mode -q -t "$TARGET"; rc=$?
  cat "$WORK/err" >&2
  [ "$rc" = 0 ] || exit "$rc"
fi
# tmux single-quoted word: literal; an embedded ' closes, is double-quoted, reopens.
tmux_quote() { local sq="'\"'\"'"; printf "'%s'" "${1//\'/$sq}"; }

# Killing a client does not withdraw a request already queued with a stopped server, so the send
# runs only if tmux can claim this one-time ticket when it executes; on timeout we revoke it first.
TICKET="$WORK/ticket"
: > "$TICKET"
SEND="send-keys -t $(tmux_quote "$TARGET")"
for key in "$@"; do
  # As argv, a trailing ';' ends the command and a trailing '\;' is a literal ';'.
  case "$key" in *'\;') key="${key%\\;};" ;; *';') key="${key%;}" ;; esac
  SEND="$SEND $(tmux_quote "$key")"
done
bounded if-shell "mv '$TICKET' '$TICKET.claimed'" "$SEND"; rc=$?
if [ "$rc" = 124 ] && ! rm "$TICKET" 2>/dev/null; then
  echo "tmux-pane-keys: the send to $TARGET was already running when the bound expired; it may have partly applied" >&2
fi
cat "$WORK/out"; cat "$WORK/err" >&2
exit "$rc"
