#!/usr/bin/env bash
# One-command launcher for the existing Program 1 acquisition CLI
# (kpi-crawler-acquire). Prepares the environment, then hands control
# straight to the real CLI:
#
#   ./run.sh <URL> [kpi-crawler-acquire arguments...]

set -euo pipefail

fail() {
    echo "run.sh: error: $*" >&2
    exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ $# -eq 0 ]]; then
    echo "Usage: ./run.sh <URL> [kpi-crawler-acquire arguments...]"
    exit 2
fi

# 1. uv must be installed.
command -v uv >/dev/null 2>&1 || fail "'uv' is not installed. See https://docs.astral.sh/uv/"

# 2. Docker must be installed and its daemon running.
command -v docker >/dev/null 2>&1 || fail "'docker' is not installed."
docker info >/dev/null 2>&1 || fail "Docker is installed but the daemon is not running."

# 3. Project dependencies. 'uv sync' is a no-op when the environment already
#    matches the lockfile, so it's safe to call unconditionally.
uv sync --dev || fail "'uv sync --dev' failed."

# 4. Load DATABASE_URL and friends from .env, if present (existing project
#    convention per AGENTS.md: cp .env.example .env).
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi
[[ -n "${DATABASE_URL:-}" ]] || fail "DATABASE_URL is not set. Copy .env.example to .env and configure it."

# 5. Ensure the PostgreSQL/pgvector container is running. 'docker compose
#    up -d' starts or reuses the project service and may recreate the
#    container if the Compose configuration or image requires it, while
#    preserving the existing project volume/data.
#
#    First though: docker-compose.yml publishes postgres on host port 5432
#    (the project's configured default). If something else already holds
#    that port, docker can't publish it there and the CLI would silently
#    talk to the wrong database.
PG_PORT=5432

port_in_use() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# 'docker compose port' only succeeds when the project's own container is
# both running and actually publishing this port, so it doubles as the
# "is our container already up and serving here" check. Only when that's
# NOT the case do we even look at what else might hold the port.
if ! docker compose port postgres "$PG_PORT" >/dev/null 2>&1 && port_in_use "$PG_PORT"; then
    if systemctl is-active --quiet postgresql; then
        echo "Port $PG_PORT is occupied by native PostgreSQL."
        read -r -p "Stop it for this run? [y/N] " reply
        case "$reply" in
            [yY]|[yY][eE][sS])
                echo "[infra] Stopping postgresql.service so the project container can use $PG_PORT..."
                echo "[sudo] password may be requested by systemctl."
                sudo systemctl stop postgresql \
                    || fail "Could not stop postgresql.service. Free port $PG_PORT and re-run."

                for _ in $(seq 1 15); do
                    port_in_use "$PG_PORT" || break
                    sleep 1
                done
                port_in_use "$PG_PORT" && fail "Port $PG_PORT is still occupied after stopping postgresql.service."
                ;;
            *)
                echo "Leaving native PostgreSQL running. Aborting."
                exit 0
                ;;
        esac
    else
        fail "Port $PG_PORT is occupied by a process that is not the native postgresql.service. Free it manually and re-run."
    fi
fi

docker compose up -d || fail "'docker compose up -d' failed."

# 6. Wait until PostgreSQL is reachable, using the same check as the
#    container's own healthcheck (docker-compose.yml).
echo "Waiting for PostgreSQL to be ready..."
attempts=30
until docker compose exec -T postgres pg_isready -U kpi_crawler -d kpi_crawler >/dev/null 2>&1; do
    attempts=$((attempts - 1))
    if [[ $attempts -le 0 ]]; then
        fail "PostgreSQL did not become ready in time."
    fi
    sleep 2
done

# 7. Apply any pending migrations.
uv run kpi-crawler migrate || fail "Database migration failed."

# 8. Ensure Playwright Chromium is available. 'playwright install' already
#    skips the download when the browser is present, so this is cheap.
uv run playwright install chromium || fail "Playwright Chromium install check failed."

# 9. Hand off to the real CLI, passing through all arguments unchanged and
#    preserving its exit code.
exec uv run kpi-crawler-acquire "$@"
