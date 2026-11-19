#!/bin/bash
# Setup script for a Claude Code cloud environment (factory/CLOUD.md).
# Paste it into the environment's "Setup script" box, or load it with "Choose file". It runs as
# root on Ubuntu 24.04 before Claude Code starts, and its result is cached only when it finishes
# in about five minutes, so it installs the toolchain and nothing that has to keep running.
# The environment copy does not follow this file: paste it again after changing it.
# No secrets here: everyone who can use the environment can read it.
set -euo pipefail

# ADAPT: install what the cloud image lacks (system packages, a version manager, an SDK).
# Pin versions and verify checksums of anything downloaded. For example:
#   apt-get update && apt-get install -y --no-install-recommends curl ca-certificates

# The clone is already on disk, but its path is not documented, so look for it. Without it the
# SessionStart hook (scripts/cloud/session-start.sh) installs everything on each new session.
# find exits non-zero on any unreadable or vanished path; that must not fail the script.
# ADAPT: replace REPO_MARKER with a file name only this repository's root has.
repo=$(find / -maxdepth 4 \( -path /proc -o -path /sys -o -path /dev -o -name .worktrees \) -prune \
    -o -name REPO_MARKER -printf '%h\n' -quit 2>/dev/null || true)
if [ -z "$repo" ]; then
    echo "Clone not found; the SessionStart hook will install the toolchain."
    exit 0
fi

cd "$repo"
# ADAPT: the same install commands as scripts/cloud/session-start.sh, so the cached snapshot
# already holds the toolchain and the packages.
echo "Toolchain and packages ready in $repo."
