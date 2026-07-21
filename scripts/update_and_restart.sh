#!/bin/bash
################################################################################
# Update and Restart Script for PBX System
#
# Pulls the latest code from git in ~/PBX, restarts the pbx systemd service,
# and tails its logs. Aborts immediately if any git step produces output
# that doesn't match what's expected for a clean update (conflicts,
# unmerged paths, detached HEAD warnings, etc.) so a bad pull never gets
# silently deployed.
#
# Usage:
#   ./update_and_restart.sh
################################################################################

set -eo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO_DIR="${HOME}/PBX"
SERVICE_NAME="pbx"

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

abort() {
    log_error "$1"
    log_error "Aborting update. No service restart was performed."
    exit 1
}

# --- Make sure we're in ~/PBX and it's a git repo ------------------------

if [ ! -d "$REPO_DIR" ]; then
    abort "Directory $REPO_DIR does not exist."
fi

cd "$REPO_DIR"

if [ ! -d ".git" ]; then
    abort "$REPO_DIR is not a git repository."
fi

log_info "Working directory: $(pwd)"

# --- git fetch -------------------------------------------------------------

log_info "Fetching from origin..."
FETCH_OUTPUT="$(git fetch origin 2>&1)" || abort "git fetch failed:
${FETCH_OUTPUT}"

if echo "$FETCH_OUTPUT" | grep -qiE 'error|fatal|rejected|cannot'; then
    abort "git fetch produced unexpected output:
${FETCH_OUTPUT}"
fi
[ -n "$FETCH_OUTPUT" ] && log_info "$FETCH_OUTPUT"

# --- git stash -u ------------------------------------------------------

log_info "Stashing local changes (including untracked files)..."
STASH_OUTPUT="$(git stash -u 2>&1)" || abort "git stash -u failed:
${STASH_OUTPUT}"

STASHED=false
if echo "$STASH_OUTPUT" | grep -qi "No local changes to save"; then
    log_info "No local changes to stash."
elif echo "$STASH_OUTPUT" | grep -qiE '^Saved working directory'; then
    STASHED=true
    log_info "$STASH_OUTPUT"
else
    abort "git stash -u produced unexpected output:
${STASH_OUTPUT}"
fi

# --- git pull ------------------------------------------------------------

log_info "Pulling latest changes..."
PULL_OUTPUT="$(git pull 2>&1)"
PULL_STATUS=$?

if [ $PULL_STATUS -ne 0 ] || echo "$PULL_OUTPUT" | grep -qiE 'conflict|error|fatal|diverged|rejected'; then
    log_error "git pull failed or produced unexpected output:
${PULL_OUTPUT}"
    if [ "$STASHED" = true ]; then
        log_warn "Your local changes are still stashed (run 'git stash pop' manually once resolved)."
    fi
    exit 1
fi
log_info "$PULL_OUTPUT"

# --- git stash pop ---------------------------------------------------------

if [ "$STASHED" = true ]; then
    log_info "Restoring stashed local changes..."
    POP_OUTPUT="$(git stash pop 2>&1)"
    POP_STATUS=$?

    if [ $POP_STATUS -ne 0 ] || echo "$POP_OUTPUT" | grep -qiE 'conflict|error|unmerged'; then
        abort "git stash pop failed or produced conflicts:
${POP_OUTPUT}
The stash was NOT dropped -- resolve manually with 'git stash list' / 'git stash pop'."
    fi
    log_info "$POP_OUTPUT"
fi

log_success "Repository updated cleanly."

# --- Restart and observe the service ----------------------------------------

log_info "Restarting ${SERVICE_NAME} service..."
sudo systemctl restart "$SERVICE_NAME"

log_info "Service status:"
sudo systemctl status "$SERVICE_NAME" --no-pager

log_info "Tailing logs (Ctrl+C to exit)..."
sudo journalctl -u "$SERVICE_NAME" -f
