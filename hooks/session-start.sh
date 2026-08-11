#!/usr/bin/env sh
# The Zen Harness — session-start hook.
#
# Opens every session with the first word and keeps the four practices present.
# Its stdout is meant to be injected into the agent's context at the start of a
# session (e.g. a Claude Code `SessionStart` hook), and it is also just readable
# output for any harness that can run a startup command. See hooks/README.md.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# 1) The first word — a fixed line, then the line of the day (bin/precept).
"$ROOT/bin/precept"

# 2) The four practices — kept present in context, every session.
echo ""
cat "$ROOT/codex-block.md"
