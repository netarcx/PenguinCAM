#!/usr/bin/env bash
# Deploy one already-tested source revision to the private UP2 Docker host.
# Run by GitHub Actions; also usable manually with UP2_DEPLOY_TARGET=UP2.

set -Eeuo pipefail

readonly DEPLOY_ROOT='/mnt/user/appdata/penguincam'
readonly REVISION="${1:-}"
readonly DEPLOY_TARGET="${UP2_DEPLOY_TARGET:-root@10.0.0.50}"
readonly SSH_KEY="${UP2_SSH_KEY:-}"

if [[ ! "$REVISION" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'deploy_up2: expected a full 40-character source revision' >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
for required in Dockerfile docker-compose.yml frc_cam_gui_app.py requirements.txt; do
  if [[ ! -f "$SOURCE_ROOT/$required" ]]; then
    echo "deploy_up2: source checkout is missing $required" >&2
    exit 2
  fi
done

ssh_options=(
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
)
if [[ -n "$SSH_KEY" ]]; then
  ssh_options+=(-i "$SSH_KEY")
fi

# Refuse before rsync can delete anything unless every persistent production asset is
# present at the one exact deployment root this script knows about.
ssh "${ssh_options[@]}" "$DEPLOY_TARGET" bash -s -- "$DEPLOY_ROOT" <<'REMOTE_PREFLIGHT'
set -Eeuo pipefail
deploy_root="$1"
[[ "$deploy_root" == '/mnt/user/appdata/penguincam' ]]
command -v docker >/dev/null
command -v rsync >/dev/null
docker compose version >/dev/null
docker network inspect cloudflare-net >/dev/null
test -f "$deploy_root/.env"
test -f "$deploy_root/config/PenguinCAM-config-2129.yaml"
mkdir -p "$deploy_root/build"
test ! -L "$deploy_root/build"
REMOTE_PREFLIGHT

ssh_command=(ssh "${ssh_options[@]}")
printf -v rsync_shell '%q ' "${ssh_command[@]}"
rsync --archive --compress --delete-delay \
  --exclude='/.git/' \
  --exclude='/.github/' \
  --exclude='/.venv/' \
  --exclude='/venv/' \
  --exclude='/.pytest_cache/' \
  --exclude='/.mypy_cache/' \
  --exclude='/.ruff_cache/' \
  --exclude='/.coverage' \
  --exclude='/htmlcov/' \
  --exclude='/.env' \
  --exclude='/*-creds.txt' \
  --exclude='/google-creds.txt' \
  --exclude='/staging-oauth' \
  --exclude='/onshape_config.json' \
  --exclude='/docker-compose.yml' \
  --exclude='/PenguinCAM-config.yaml' \
  --exclude='/PenguinCAM-config-*.yaml' \
  --exclude='/PenguinCAM-config.yml' \
  --exclude='/PenguinCAM-config-*.yml' \
  --exclude='/new-config.yaml' \
  --exclude='/penguincam-jobs/' \
  --exclude='/__pycache__/' \
  --exclude='/**/__pycache__/' \
  --exclude='/*.nc' \
  --exclude='/*.gcode' \
  --exclude='/*.bak' \
  --exclude='/*.backup' \
  --rsh="$rsync_shell" \
  "$SOURCE_ROOT/" "$DEPLOY_TARGET:$DEPLOY_ROOT/build/"

scp "${ssh_options[@]}" "$SOURCE_ROOT/docker-compose.yml" \
  "$DEPLOY_TARGET:$DEPLOY_ROOT/docker-compose.next.yml"

ssh "${ssh_options[@]}" "$DEPLOY_TARGET" bash -s -- "$DEPLOY_ROOT" "$REVISION" <<'REMOTE_DEPLOY'
set -Eeuo pipefail
deploy_root="$1"
revision="$2"
service='penguincam'
live_compose="$deploy_root/docker-compose.yml"
next_compose="$deploy_root/docker-compose.next.yml"
previous_compose="$deploy_root/docker-compose.previous.yml"

[[ "$deploy_root" == '/mnt/user/appdata/penguincam' ]]
cd "$deploy_root"
trap 'rm -f "$next_compose"' EXIT

# Validate the candidate against the production .env before changing the live file.
docker compose --project-directory "$deploy_root" -f "$next_compose" config --quiet

previous_image="$(docker image inspect --format '{{.Id}}' penguincam:local 2>/dev/null || true)"
if [[ -n "$previous_image" ]]; then
  docker image tag "$previous_image" penguincam:rollback
fi
if [[ -f "$live_compose" ]]; then
  cp -p "$live_compose" "$previous_compose"
fi

# The running container stays on the old image while the replacement is built.
docker compose --project-directory "$deploy_root" -f "$next_compose" build --pull \
  --build-arg "GIT_SHA=$revision" "$service"

built_revision="$(docker image inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' penguincam:local)"
if [[ "$built_revision" != "$revision" ]]; then
  echo "deploy_up2: built image reports $built_revision, expected $revision" >&2
  exit 1
fi

install -m 0644 "$next_compose" "$live_compose"

status='missing'
wait_for_health() {
  local wait_seconds="$1"
  local deadline=$((SECONDS + wait_seconds))
  while (( SECONDS < deadline )); do
    status="$(docker inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$service" 2>/dev/null || true)"
    case "$status" in
      healthy) return 0 ;;
      exited|dead) return 1 ;;
    esac
    sleep 3
  done
  return 1
}

if docker compose up -d --force-recreate --remove-orphans "$service" \
    && wait_for_health 150; then
  echo "UP2 now runs UV-CAM revision $revision"
  docker image rm penguincam:rollback >/dev/null 2>&1 || true
  exit 0
fi

echo "deploy_up2: replacement did not become healthy; status=$status" >&2
docker compose logs --no-color --tail=120 "$service" >&2 || true

if [[ -n "$previous_image" ]]; then
  echo 'deploy_up2: restoring the previous image and Compose file' >&2
  docker image tag penguincam:rollback penguincam:local
  if [[ -f "$previous_compose" ]]; then
    install -m 0644 "$previous_compose" "$live_compose"
  fi
  if docker compose up -d --force-recreate --remove-orphans "$service" \
      && wait_for_health 90; then
    echo 'deploy_up2: rollback is healthy' >&2
  else
    echo "deploy_up2: CRITICAL - rollback is not healthy; status=$status" >&2
    docker compose logs --no-color --tail=120 "$service" >&2 || true
  fi
else
  echo 'deploy_up2: no previous image exists; automatic rollback is unavailable' >&2
fi
exit 1
REMOTE_DEPLOY
