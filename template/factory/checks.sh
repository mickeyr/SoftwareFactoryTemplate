#!/usr/bin/env bash
# The repo's checks. Exit 0 only when the branch is fit to review.
#
# The gate runs this script from the main checkout with the job's worktree as
# the working directory. Keep it fast, deterministic and non-interactive: no
# watch modes, no prompts, nothing that rewrites files.
#
# Several jobs run it at once, each in its own worktree. Turn off any build
# daemon or shared compiler server here (export the tool's variable for it), or
# one worktree's build can be served by another's; the gate fails a run whose
# output names another job's worktree.
#
# INSTALL.md replaces the lines below with this project's real commands, e.g.:
#
#   npm ci --prefer-offline
#   npm run lint
#   npm run typecheck
#   npm test -- --run
#   npm run build
set -euo pipefail

echo "factory/checks.sh has no checks yet. Add this repo's test, lint, typecheck and build commands." >&2
exit 1
