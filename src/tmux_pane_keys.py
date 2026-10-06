"""argv that sends keys into an agent pane through src/tmux-pane-keys.sh, which leaves any pane mode
first and bounds the send; Python callers run it with their own runner."""
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "tmux-pane-keys.sh"
# The script bounds the mode exit and the send at 5 s each.
TIMEOUT_S = 12


def argv(socket, target, *keys, tmux="tmux"):
    return ["bash", str(SCRIPT), "--tmux", str(tmux), "-S", str(socket), "-t", target, "--", *keys]
