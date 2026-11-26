#!/usr/bin/env bash
# Deploy Amir Beckett: rsync this repo to the server, then build and start the container there.
#
# Usage: scripts/deploy.sh [--dry-run] [--allow-publishing]
#
#   --dry-run            rsync --dry-run: it still connects to the server to compare, but
#                        copies nothing and runs no docker command (it prints that command).
#   --allow-publishing   required when config/amir.toml turns publishing on, which the owner
#                        does only after approving the card previews.
#
# DEPLOY_SERVER (user@host) is the target: from the environment, else the line DEPLOY_SERVER=...
# in the git-ignored .env.deploy. There is no default. AMIR_DEPLOY_DIR is the server folder it
# syncs into: from the environment, else the line AMIR_DEPLOY_DIR=<path> in the git-ignored
# scripts/.deploy.env. There is no default either; no server path is written in the repo.
# The image is built on the server, where it runs. The server's .env (the bot token) is never
# copied, overwritten or deleted, and neither is anything under data/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${DEPLOY_SERVER:-}" && -f "$ROOT/.env.deploy" ]]; then
    DEPLOY_SERVER="$(sed -n 's/^DEPLOY_SERVER=//p' "$ROOT/.env.deploy" | tail -n 1)"
fi
if [[ -z "${AMIR_DEPLOY_DIR:-}" && -f "$ROOT/scripts/.deploy.env" ]]; then
    AMIR_DEPLOY_DIR="$(sed -n 's/^AMIR_DEPLOY_DIR=//p' "$ROOT/scripts/.deploy.env" | tail -n 1)"
fi
DEPLOY_DIR="${AMIR_DEPLOY_DIR:-}"
CONFIG="$ROOT/config/amir.toml"
PYTHON="$ROOT/.venv/bin/python"
PUBLISHING_OFF="Publishing: OFF (previews only)"
ALL_IDS_SET="Missing runtime IDs: none"
die() {
    echo "deploy: $*" >&2
    exit 1
}

[[ -n "$DEPLOY_DIR" ]] || die "no server folder: set AMIR_DEPLOY_DIR, or put \
AMIR_DEPLOY_DIR=<path> in scripts/.deploy.env (git-ignored). Nothing was deployed."
# The folder goes unquoted into the remote shell (so a leading ~ expands there): plain path
# characters only, and not the server's root.
[[ "$DEPLOY_DIR" =~ ^[A-Za-z0-9_.~/-]+$ && "$DEPLOY_DIR" != "/" ]] || die "AMIR_DEPLOY_DIR must \
be a plain path (letters, digits, _ . ~ / -), not /. Nothing was deployed."

# Rebuild, keep the image that was running as amir-beckett:previous (for a rollback) only when
# the build produced a different image, so a rebuild that changes nothing never overwrites the
# rollback image with the current one; then start a new container. The outgoing image is tagged
# :outgoing by name before the build: the server's containerd image store no longer finds an
# image by its id once the build has moved :latest off it, even while its container runs. The
# :outgoing tag is dropped afterwards (only the tag; the image stays, as :previous or :latest).
# --force-recreate is required: config/amir.toml is a single-file bind mount, and rsync replaces
# the file (a new inode), so a running container keeps reading the old one. A config-only change
# (publishing on or off) leaves the image and compose.yaml unchanged, and without
# --force-recreate compose would keep that container.
REMOTE_COMMAND="cd $DEPLOY_DIR && "'had_old=0 && if docker image inspect amir-beckett:latest >/dev/null 2>&1; then docker image tag amir-beckett:latest amir-beckett:outgoing && had_old=1; fi && docker compose build && if [ "$had_old" = 1 ]; then if [ "$(docker image inspect -f "{{.Id}}" amir-beckett:outgoing)" != "$(docker image inspect -f "{{.Id}}" amir-beckett:latest)" ]; then docker image tag amir-beckett:outgoing amir-beckett:previous; fi && docker image rm amir-beckett:outgoing >/dev/null; fi && docker compose up -d --force-recreate'


dry_run=0
allow_publishing=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        --allow-publishing) allow_publishing=1 ;;
        -h | --help)
            sed -n '2,16p' "$0"
            exit 0
            ;;
        *)
            echo "deploy: unknown option: $arg (see --help)" >&2
            exit 2
            ;;
    esac
done

[[ -n "${DEPLOY_SERVER:-}" ]] || die "no server: set DEPLOY_SERVER, or put DEPLOY_SERVER=user@host \
in .env.deploy (git-ignored). Nothing was deployed."
[[ -f "$CONFIG" ]] || die "config/amir.toml not found. Copy config/amir.example.toml to \
config/amir.toml and set the five Discord IDs; it becomes the server's config and is never \
committed."
[[ -x "$PYTHON" ]] || die "the project's Python is missing (.venv); run 'uv sync' first. It \
checks config/amir.toml before anything is copied."

# Amir's own loader reads the config, so the check means exactly what the bot will do. The
# placeholder token only satisfies the loader; the real one stays on the server.
if ! report="$(DISCORD_TOKEN=deploy-check AMIR_CONFIG="$CONFIG" "$PYTHON" -m amir --check-config 2>&1)"; then
    echo "$report" >&2
    die "config/amir.toml is not valid; nothing was deployed."
fi
echo "$report"

if ! grep -qxF "$PUBLISHING_OFF" <<<"$report"; then
    if [[ $allow_publishing -ne 1 ]]; then
        die "config/amir.toml turns publishing ON, so Amir would post to Discord channels. \
That needs the owner's approval of the card previews first (README, 'Turning publishing on'). \
Once approved, run again with --allow-publishing. Nothing was deployed."
    fi
    echo "deploy: publishing is ON (--allow-publishing given): Amir will post to Discord." >&2
fi

if ! grep -qxF "$ALL_IDS_SET" <<<"$report"; then
    missing="$(grep '^Missing runtime IDs: ' <<<"$report" || true)"
    die "config/amir.toml must set all five Discord IDs (${missing:-Missing runtime IDs: unknown}); \
the bot refuses to start without them. Nothing was deployed."
fi

# --delete removes files deleted here (a removed greeting clip, say), but never an excluded path
# on the server. A leading "/" anchors a pattern at the repo root; no trailing "/", so a
# symlinked .venv is excluded as well as a real one. "/.*/" keeps every hidden folder at the root
# (git, tool caches, local working state) off the server; nothing tracked lives in one. The first matching rule wins, so the
# .env.example include must come before the .env* exclude (.env, .env.local, .envrc ...).
# --chmod makes every synced file world-readable (folders searchable), so the container's UID
# 10001 can read config/amir.toml and the clips whatever their local mode. None of it is secret:
# the token never leaves the server's own .env.
rsync_args=(
    --archive --compress --delete --chmod=Do+rx,Fo+r
    --include=/.env.example
    '--exclude=.env*'
    --exclude=/.git
    --exclude=/.venv
    --exclude=/data
    --exclude=/docs/previews
    '--exclude=/.*/'
    --exclude=__pycache__
    --exclude=/.pytest_cache
    --exclude=/.ruff_cache
    --exclude=/.coverage
)
if [[ $dry_run -eq 1 ]]; then
    rsync_args+=(--dry-run --itemize-changes)
fi

echo "deploy: syncing $ROOT/ to $DEPLOY_SERVER:$DEPLOY_DIR/"
rsync "${rsync_args[@]}" "$ROOT/" "$DEPLOY_SERVER:$DEPLOY_DIR/"

if [[ $dry_run -eq 1 ]]; then
    echo "deploy: dry run; would now run: ssh $DEPLOY_SERVER '$REMOTE_COMMAND'"
    exit 0
fi
echo "deploy: building and starting on $DEPLOY_SERVER"
ssh "$DEPLOY_SERVER" "$REMOTE_COMMAND"
echo "deploy: done. Logs: ssh $DEPLOY_SERVER 'cd $DEPLOY_DIR && docker compose logs --tail=50'"
