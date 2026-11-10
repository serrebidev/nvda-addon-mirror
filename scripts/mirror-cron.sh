#!/bin/bash
# Scheduled NVDA add-on mirror refresh: pull, test, build, audit, deploy.
#
# Runs on the owner's server from a systemd timer (see scripts/systemd/).
# Build caches live in the repo directory, so unchanged add-ons are not
# re-downloaded between runs. Failures abort the deploy; the live site keeps
# serving the last good build.
#
# Environment (all optional):
#   MIRROR_REPO_DIR   repo checkout (default /opt/nvda-addon-mirror)
#   MIRROR_WEBROOT    deployed site root (default /var/www/nvda-addon-mirror)
#   MIRROR_LOG        log file (default /var/log/nvda-addon-mirror.log)
#   FORGEJO_TOKEN_FILE  file holding a Forgejo token for the
#                       translation-gap issue sync (default
#                       /root/.config/nvda-addon-mirror/forgejo-token)
set -u

REPO_DIR="${MIRROR_REPO_DIR:-/opt/nvda-addon-mirror}"
WEBROOT="${MIRROR_WEBROOT:-/var/www/nvda-addon-mirror}"
LOG="${MIRROR_LOG:-/var/log/nvda-addon-mirror.log}"
TOKEN_FILE="${FORGEJO_TOKEN_FILE:-/root/.config/nvda-addon-mirror/forgejo-token}"
LOCK_FILE="$REPO_DIR/.mirror-cron.lock"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

exec >>"$LOG" 2>&1
log "--- refresh start ---"

# One run at a time; a second trigger while a build runs is skipped, and the
# next timer tick retries.
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
# hour cannot cover the scan). Reuses the host's existing gh OAuth token,
# but only after the API accepts it: a dead token fails every call, while
# no token lets the build fall back to its reduced unauthenticated scan.
if [ -z "${GITHUB_TOKEN:-}" ] && [ -f /root/.config/gh/hosts.yml ]; then
    CANDIDATE="$(awk '/oauth_token:/ {print $2; exit}' /root/.config/gh/hosts.yml)"
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

log "running tests"
# Tests never see production secrets: a test that reaches for VT_API_KEY
# would spend the real VirusTotal quota.
if ! env -u VT_API_KEY python3 -W error -m unittest discover -s tests; then
    log "TESTS FAILED; keeping the last deployed site"
    exit 1
fi

log "building mirror"
REDUCED=0
if ! python3 mirror.py --out public; then
    # The usual cause is GitHub API quota during the author backfill: a
    # failed run still saves the release states it verified (see
    # fetch_github_owners), so retries converge. Meanwhile publish what
    # verified rather than nothing; stats.json records the reduced source
    # list and the backfill notice below says so on the human pages.
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
    # No issue token: the log lines are the queue of record.
    python3 -c 'import json, translation_issue
translation_issue.log_queue(json.load(open("findings.json")))' || true
fi

# Automatic translation fill via the owner's muse.ai agent (report-only).
# Only missing fields are ever added; existing human text is never touched.
# Needs push access so the result commits for the next build to publish.
PUSH_TOKEN_FILE="${FORGEJO_PUSH_TOKEN_FILE:-/root/.config/nvda-addon-mirror/forgejo-push-token}"
if [ -f findings.json ] && [ -f "$PUSH_TOKEN_FILE" ]; then
    FORGEJO_PUSH_TOKEN="$(cat "$PUSH_TOKEN_FILE")"
    export FORGEJO_PUSH_TOKEN
    python3 scripts/muse_translate.py findings.json --commit || true
else
    log "muse.ai translation fill skipped (no findings or no push token)"
fi

log "deploying to $WEBROOT"
mkdir -p "$WEBROOT"
if ! rsync -a --delete --exclude '.well-known' public/ "$WEBROOT"/; then
    log "DEPLOY FAILED"
    exit 1
fi
if [ "$REDUCED" = "1" ]; then
    python3 scripts/backfill_banner.py "$WEBROOT" || true
fi

ACCEPTED="$(python3 -c 'import json; print(json.load(open("public/stats.json"))["accepted"])' 2>/dev/null || echo '?')"
log "refresh done; $ACCEPTED add-ons deployed"
