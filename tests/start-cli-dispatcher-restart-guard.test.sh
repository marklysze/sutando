#!/usr/bin/env bash
# The dispatcher refuses an in-session restart before it reaps any helper: a Codex
# core asked to switch to Claude from inside its own session keeps its watcher and
# observer. Unmarked and overridden callers still reap and delegate.
# tmux and the runtime launcher are stubs; nothing real is touched.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TD="$(mktemp -d)"; trap 'rm -rf "$TD"' EXIT
fails=0
say() { echo "$1  $2"; [ "$1" = FAIL ] && fails=$((fails+1)); return 0; }

FIX="$TD/repo"; mkdir -p "$FIX/src/agent/claude/cli" "$FIX/bin"
cp "$REPO/src/agent/start-cli.sh" "$REPO/src/agent/restart-guard.sh" "$FIX/src/agent/"
printf '#!/bin/bash\necho "LAUNCHER $*" >> "$TMUX_LOG"\n' > "$FIX/src/agent/claude/cli/start-cli.sh"
cat > "$FIX/bin/tmux" <<'EOF'
#!/bin/bash
echo "$*" >> "$TMUX_LOG"
case "$*" in
  *has-session*) exit 0 ;;
  *show-environment*SUTANDO_CORE_RUNTIME*) echo SUTANDO_CORE_RUNTIME=codex ;;
esac
exit 0
EOF
chmod +x "$FIX/bin/tmux" "$FIX/src/agent/claude/cli/start-cli.sh"

# $@ = extra env; the live core runs Codex and this launch asks for Claude.
run() {
  : > "$TD/log"
  env -i PATH="$FIX/bin:/usr/bin:/bin" HOME="$TD" TMUX_LOG="$TD/log" SUTANDO_TMUX_SOCKET="$TD/sock" "$@" \
    /bin/bash "$FIX/src/agent/start-cli.sh" --runtime claude > "$TD/out" 2> "$TD/err" < /dev/null
  rc=$?
}

run SUTANDO_CORE_SESSION=1
if [ "$rc" != 0 ] && ! grep -q kill-session "$TD/log" && ! grep -q LAUNCHER "$TD/log" \
   && grep -q "refusing --restart from inside the sutando-core session" "$TD/err"; then
  say PASS "inherited core marker: refused, no watcher/observer/core kill, no launcher"
else
  say FAIL "inherited core marker: rc=$rc log=$(tr '\n' '|' < "$TD/log")"
fi

run
if [ "$rc" = 0 ] && grep -q "kill-session -t =sutando-core-watcher" "$TD/log" \
   && grep -q "kill-session -t =sutando-core-observer" "$TD/log" && grep -q "LAUNCHER --restart" "$TD/log"; then
  say PASS "unmarked caller: helpers reaped, restart delegated"
else
  say FAIL "unmarked caller: rc=$rc log=$(tr '\n' '|' < "$TD/log")"
fi

run SUTANDO_CORE_SESSION=1 SUTANDO_ALLOW_INSESSION_RESTART=1
if [ "$rc" = 0 ] && grep -q "kill-session -t =sutando-core-observer" "$TD/log" && grep -q "LAUNCHER --restart" "$TD/log"; then
  say PASS "explicit override: helpers reaped, restart delegated"
else
  say FAIL "explicit override: rc=$rc log=$(tr '\n' '|' < "$TD/log")"
fi

[ "$fails" = 0 ] && echo "all dispatcher restart-guard checks pass" || { echo "$fails failed"; exit 1; }
