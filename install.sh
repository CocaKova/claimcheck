#!/bin/sh
# claimcheck installer — https://claimcheck.cc
#   curl -fsSL https://claimcheck.cc/install.sh | sh
# Installs the `claimcheck` command with whatever Python tool manager is present (uv, pipx, or pip
# --user), then wires every supported agent found in this home folder. Re-running is safe.
# Set CLAIMCHECK_SOURCE to a git URL or local path to install from there instead of PyPI.
set -eu

PKG="${CLAIMCHECK_SOURCE:-claimcheck-receipts}"
say() { printf '%s\n' "$*"; }

if ! command -v python3 >/dev/null 2>&1; then
  say "claimcheck needs python3 (3.10 or newer). Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  say "installing with uv…"; uv tool install --force "$PKG" >/dev/null
elif command -v pipx >/dev/null 2>&1; then
  say "installing with pipx…"; pipx install --force "$PKG" >/dev/null
else
  say "installing with pip (user)…"
  python3 -m pip install --user --upgrade "$PKG" >/dev/null 2>&1 || python3 -m pip install --user --upgrade --break-system-packages "$PKG" >/dev/null
fi

# make sure the command is reachable for this run even if the shell profile has not picked it up yet
for d in "$HOME/.local/bin" "$HOME/Library/Python/3.12/bin" "$HOME/Library/Python/3.13/bin" "$HOME/Library/Python/3.14/bin"; do
  case ":$PATH:" in *":$d:"*) ;; *) [ -d "$d" ] && PATH="$d:$PATH" ;; esac
done
export PATH

if command -v claimcheck >/dev/null 2>&1; then
  claimcheck init
  say ""
  say "done. Start a new agent session; from its first tool-using turn on, receipts land in ~/.claimcheck/receipts/"
  say "   claimcheck open      — see the latest receipt      claimcheck flagged  — the ones worth a look"
  say "   claimcheck doctor    — is everything wired          claimcheck init --remove — undo"
else
  say "installed, but the 'claimcheck' command is not on your PATH yet. Open a new terminal and run: claimcheck init"
fi
