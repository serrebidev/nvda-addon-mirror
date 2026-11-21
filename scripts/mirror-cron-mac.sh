#!/bin/bash
# Scheduled NVDA add-on mirror refresh on the owner's Mac: pull, test,
# build, audit, deploy to the VPS webroot over SSH.
#
# Mac counterpart of scripts/mirror-cron.sh (which stays for the VPS as a
# manual fallback; its systemd timer is disabled while the Mac is primary,
# so the two never spend GitHub/VirusTotal quota at the same time).
# Runs from launchd (scripts/launchd/com.serrebi.nvda-mirror-refresh.plist).
#
# Environment (all optional):
#   MIRROR_REPO_DIR     repo checkout (default ~/nvda-addon-mirror)
#   MIRROR_DEPLOY_HOST  ssh target holding the webroot (default root@100.103.9.33)
#   MIRROR_WEBROOT      deployed site root on that host (default /var/www/my_webapp/www)
#   MIRROR_LOG          log file (default ~/Library/Logs/nvda-addon-mirror.log)
#   MIRROR_ENV_FILE     file with VT_API_KEY etc (default ~/.config/nvda-addon-mirror/env)
set -u
export PATH="/opt/homebrew/bin:/opt/homebrew/opt/util-linux/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO_DIR="${MIRROR_REPO_DIR:-$HOME/nvda-addon-mirror}"
DEPLOY_HOST="${MIRROR_DEPLOY_HOST:-root@100.103.9.33}"
WEBROOT="${MIRROR_WEBROOT:-/var/www/my_webapp/www}"
LOG="${MIRROR_LOG:-$HOME/Library/Logs/nvda-addon-mirror.log}"
ENV_FILE="${MIRROR_ENV_FILE:-$HOME/.config/nvda-addon-mirror/env}"
TOKEN_FILE="${FORGEJO_TOKEN_FILE:-$HOME/.config/nvda-addon-mirror/forgejo-token}"
PUSH_TOKEN_FILE="${FORGEJO_PUSH_TOKEN_FILE:-$HOME/.config/nvda-addon-mirror/forgejo-push-token}"
DEPLOY_KEY="${MIRROR_DEPLOY_KEY:-$HOME/.ssh/nvda-mirror_ed25519}"
LOCK_FILE="$REPO_DIR/.mirror-cron.lock"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
log "--- refresh start (mac) ---"

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi
# Values from the env file name the deploy target, not the local disk.
DEPLOY_HOST="${MIRROR_DEPLOY_HOST:-$DEPLOY_HOST}"
WEBROOT="${MIRROR_WEBROOT:-$WEBROOT}"

# One run at a time; a second trigger while a build runs is skipped, and the
# next launchd tick retries.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "another refresh is running; skipping"
    exit 0
fi

cd "$REPO_DIR" || { log "cannot cd to $REPO_DIR"; exit 1; }

log "pulling latest main"
if ! git pull --ff-only origin main; then
    log "git pull failed; building from the current tree"
fi

# GitHub API access for author discovery (60 unauthenticated requests an
# hour cannot cover the scan). Reuses the host's gh OAuth token, but only
# after the API accepts it, exactly as the VPS script does.
if [ -z "${GITHUB_TOKEN:-}" ] && [ -f "$HOME/.config/gh/hosts.yml" ]; then
    CANDIDATE="$(awk '/oauth_token:/ {print $2; exit}' "$HOME/.config/gh/hosts.yml")"
    if [ -n "$CANDIDATE" ] && [ "$(curl -s --max-time 20 -o /dev/null \
        -w '%{http_code}' -H "Authorization: Bearer $CANDIDATE" \
        https://api.github.com/rate_limit)" = "200" ]; then
        GITHUB_TOKEN="$CANDIDATE"
        export GITHUB_TOKEN
        log "GitHub API token accepted"
    else
        log "stored GitHub token rejected by the API; building unauthenticated"
    fi
fi

# A Forgejo push token doubles as the API token for pinned/owner reads when
# no dedicated token is configured (public releases need none either way).
if [ -z "${FORGEJO_TOKEN:-}" ] && [ -f "$PUSH_TOKEN_FILE" ]; then
    FORGEJO_TOKEN="$(cat "$PUSH_TOKEN_FILE")"
    export FORGEJO_TOKEN
fi

log "running tests"
# Tests never see production secrets: a test that reaches for VT_API_KEY
# would spend the real VirusTotal quota.
if ! env -u VT_API_KEY -u MIRROR_VT_CALLS_PER_BUILD python3 -W error -m unittest discover -s tests; then
    log "TESTS FAILED; keeping the last deployed site"
    exit 1
fi

log "building mirror"
REDUCED=0
if ! python3 mirror.py --out public; then
    log "FULL BUILD FAILED; retrying without the github_owner source"
    if ! python3 mirror.py --out public \
        --sources official,bestmidi,ru,es,forgejo_owner,pinned; then
        log "BUILD FAILED; keeping the last deployed site"
        exit 1
    fi
    REDUCED=1
fi

log "auditing catalog"
if ! python3 -c 'import sys, mirror
problems = mirror.audit_catalog("public")
for problem in problems:
    print(problem)
if problems:
    sys.exit(1)
print("Catalog audit passed.")'; then
    log "AUDIT FAILED; keeping the last deployed site"
    exit 1
fi

# Report-only translation queue: never blocks the deploy.
log "refreshing translation overlay (report-only)"
python3 auto_translate.py --addons public/addons.json || true
cp autoTranslations.json autoTranslationCache.json public/ 2>/dev/null || true
log "auditing translations (report-only)"
python3 audit_translations.py --addons public/addons.json --json findings.json || true
if [ -f "$TOKEN_FILE" ]; then
    FORGEJO_TOKEN="$(cat "$TOKEN_FILE")"
    export FORGEJO_TOKEN
    python3 translation_issue.py sync findings.json || true
elif [ -f findings.json ]; then
    # No issue token: the log lines are the queue of record (same as VPS).
    python3 -c 'import json, translation_issue
translation_issue.log_queue(json.load(open("findings.json")))' || true
fi

# Automatic translation fill via the owner's muse.ai agent (report-only).
# Only missing fields are ever added; existing human text is never touched.
if [ -f findings.json ] && [ -f "$PUSH_TOKEN_FILE" ]; then
    FORGEJO_PUSH_TOKEN="$(cat "$PUSH_TOKEN_FILE")"
    export FORGEJO_PUSH_TOKEN
    python3 scripts/muse_translate.py findings.json --commit || true
else
    log "muse.ai translation fill skipped (no findings or no push token)"
fi

log "deploying to $DEPLOY_HOST:$WEBROOT"
if ! rsync -a --delete --exclude '.well-known' \
    -e "ssh -i $DEPLOY_KEY -o BatchMode=yes" \
    public/ "$DEPLOY_HOST:$WEBROOT"/; then
    log "DEPLOY FAILED"
    exit 1
fi
if [ "$REDUCED" = "1" ]; then
    # backfill_banner.py edits a local webroot; on the Mac the reduced-build
    # banner cannot be applied remotely, so the log line above is the record.
    log "reduced build deployed (github_owner skipped)"
fi

ACCEPTED="$(python3 -c 'import json; print(json.load(open("public/stats.json"))["accepted"])' 2>/dev/null || echo '?')"
log "refresh done; $ACCEPTED add-ons deployed"
