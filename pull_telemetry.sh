#!/usr/bin/env bash
#
# pull_cpc_telemetry.sh
# Pull telemetry data from the remote CPC host with rsync.
#
# Usage:   ./pull_cpc_telemetry.sh
# Cron:    */15 * * * * /path/to/pull_cpc_telemetry.sh >> /var/log/cpc_pull.log 2>&1
#

set -euo pipefail

# ---- Configuration ---------------------------------------------------------
REMOTE_HOST="cpc.remote"
REMOTE_USER="omar"
REMOTE_DIR="/home/omar/aq/omarcpc/local/telemetry"
LOCAL_DIR="/Users/eikl/Documents/Projects/CPC/data"          # where data lands locally
SSH_KEY=""                                     # e.g. "${HOME}/.ssh/id_ed25519"; leave empty to use default
LOCKFILE="/tmp/pull_cpc_telemetry.lock"
# ---------------------------------------------------------------------------

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# flock isn't a macOS builtin (it ships via Homebrew's util-linux); launchd/cron
# give this script a minimal PATH that won't include it, and a missing command
# looks identical to a busy lock to `if ! flock ...; then` (both non-zero exit),
# silently turning every run into a same-as-before no-op. Widen PATH and fail
# loudly instead of guessing.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:${PATH}"
if ! command -v flock >/dev/null 2>&1; then
    log "flock not found on PATH (${PATH}); cannot safely prevent overlapping runs, aborting."
    exit 1
fi

# Prevent overlapping runs (important under cron)
exec 9>"${LOCKFILE}"
if ! flock -n 9; then
    log "Another instance is already running; exiting."
    exit 0
fi

mkdir -p "${LOCAL_DIR}"

# Build the ssh transport. BatchMode=yes prevents hanging on a password prompt
# during unattended (cron) runs; ConnectTimeout avoids indefinite stalls.
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30"
if [[ -n "${SSH_KEY}" ]]; then
    SSH_OPTS="${SSH_OPTS} -i ${SSH_KEY}"
fi

log "Starting rsync pull from ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"

# Note the trailing slash on the source: copies the *contents* of telemetry/
# into LOCAL_DIR rather than nesting a telemetry/ subdirectory.
#   -a  archive (recursive, preserve times/perms/symlinks)
#   -z  compress in transit
#   -h  human-readable sizes
#   --partial    keep partially transferred files (resume large files)
#   --info=progress2  overall progress
rsync -azh --partial  \
    -e "ssh ${SSH_OPTS}" \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/" \
    "${LOCAL_DIR}/"

rc=$?
if [[ ${rc} -eq 0 ]]; then
    log "Pull completed successfully into ${LOCAL_DIR}"
else
    log "rsync failed with exit code ${rc}"
fi

exit ${rc}
