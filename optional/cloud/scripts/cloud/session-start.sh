#!/bin/bash
# SessionStart hook (.claude/settings.json) for Claude Code cloud sessions (factory/CLOUD.md).
# Hooks also run in local sessions, so it exits at once unless CLAUDE_CODE_REMOTE is "true",
# which only cloud sessions set.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
set -euo pipefail

cd "$CLAUDE_PROJECT_DIR"

# A hook's stdout goes into Claude's context, so the install output goes to a log instead.
log=/tmp/session-start.log
# set -e does not apply on the left of ||, so && is what stops at the first failing step.
{
    # ADAPT: replace `false` with the commands that install this branch's toolchain and
    # dependencies, joined with &&. They must be quick when nothing changed: the environment's
    # setup script (scripts/cloud/setup.sh) already ran them once. For example:
    #   mise trust --quiet mise.toml && mise install --locked && mise run setup
    #   npm ci --prefer-offline
    false
} > "$log" 2>&1 || {
    echo "Toolchain setup failed; the output is in $log." >&2
    exit 1
}

# ADAPT: if the toolchain lives outside PATH (mise shims, a venv), add it for the commands
# Claude runs later in the session, or delete this block. For example:
#   echo "export PATH=\"$HOME/.local/share/mise/shims:\$PATH\"" >> "$CLAUDE_ENV_FILE"

echo "Toolchain and dependencies ready ($log)."
