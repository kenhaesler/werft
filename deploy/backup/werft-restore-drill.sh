#!/usr/bin/env bash
# Monthly Werft restore drill (SPEC §2/§10; lineage §10.6): a backup that has
# never been restored is not a backup. Restores the newest local pg_dump into
# a throwaway postgres container (same image digest as production, sourced
# from the deployed .env) and runs canary queries against it.
#
# Host-side, run as root by werft-restore-drill.service/.timer.
set -euo pipefail
ENV_FILE=/opt/werft/compose/.env
DUMPS=/opt/werft/backups/dumps
NTFY_URL_FILE=/opt/werft/secrets/ntfy_url        # optional; alert on failure

notify() {
    [ -r "$NTFY_URL_FILE" ] || return 0
    curl -fsS -m 10 -d "$1" "$(cat "$NTFY_URL_FILE")" >/dev/null || true
}
trap 'notify "werft-restore-drill FAILED on $(hostname): line $LINENO"' ERR

[ -r "$ENV_FILE" ] || { notify "werft-restore-drill: $ENV_FILE unreadable"; exit 1; }
POSTGRES_IMAGE=$(grep -m1 '^POSTGRES_IMAGE=' "$ENV_FILE" | cut -d= -f2- || true)
[ -n "$POSTGRES_IMAGE" ] || { notify "werft-restore-drill: POSTGRES_IMAGE not set in $ENV_FILE"; exit 1; }

DUMP=$(ls -1t "$DUMPS"/werft-*.dump 2>/dev/null | head -1 || true)
[ -n "$DUMP" ] || { notify "werft-restore-drill: no dumps found in $DUMPS"; exit 1; }

NAME="werft-restore-drill-$$"
PASSWORD=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)

# From here on the throwaway container may exist, so the ERR trap widens to
# also clean it up; EXIT covers the success path (docker run --rm already
# self-removes on stop, this trap makes stop-then-remove unconditional).
# On an error exit, both the ERR trap and the EXIT trap fire (bash runs ERR,
# then still runs EXIT on the way out) — cleanup() double-fires by design.
# That's fine: `docker rm -f` is idempotent against an already-removed
# container (second call just fails quietly into the `|| true`).
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap 'cleanup; notify "werft-restore-drill FAILED on $(hostname): line $LINENO"' ERR
trap cleanup EXIT

docker run -d --rm --name "$NAME" \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_DB=postgres \
    "$POSTGRES_IMAGE" >/dev/null

# Bounded wait for the throwaway instance to accept connections (60 x 2s = 2min).
i=0
until docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        notify "werft-restore-drill: throwaway postgres never became ready"
        exit 1
    fi
    sleep 2
done

docker exec -i "$NAME" pg_restore -U postgres -d postgres --no-owner --no-privileges < "$DUMP"

RUNS_COUNT=$(docker exec "$NAME" psql -U postgres -d postgres -tAc 'SELECT count(*) FROM runs;')
[ -n "$RUNS_COUNT" ] || { notify "werft-restore-drill: SELECT count(*) FROM runs; returned nothing"; exit 1; }

ALEMBIC_ROWS=$(docker exec "$NAME" psql -U postgres -d postgres -tAc 'SELECT count(*) FROM alembic_version;')
[ "$ALEMBIC_ROWS" = "1" ] || { notify "werft-restore-drill: alembic_version has $ALEMBIC_ROWS rows, expected 1"; exit 1; }

notify "restore drill OK, $RUNS_COUNT runs"
