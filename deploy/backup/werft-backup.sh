#!/usr/bin/env bash
# Nightly Werft backup (SPEC §2/§10; lineage §10.6): pg_dump -Fc + restic offsite.
# Retention 14 daily + 8 weekly. D9: the GitHub App key is EXCLUDED (re-creatable).
#
# Host-side, run as root by werft-backup.service/.timer. Must work when the
# manager container is broken or the whole stack is down: only postgres needs
# to be up for the dump half, and restic still pushes whatever dumps already
# exist on disk even if pg_isready never succeeds this run.
set -euo pipefail
COMPOSE=/opt/werft/compose/compose.yaml
DUMPS=/opt/werft/backups/dumps
NTFY_URL_FILE=/opt/werft/secrets/ntfy_url        # optional; alert on failure
export RESTIC_REPOSITORY_FILE=/opt/werft/secrets/restic_repo
export RESTIC_PASSWORD_FILE=/opt/werft/secrets/restic_password

notify() {
    [ -r "$NTFY_URL_FILE" ] || return 0
    curl -fsS -m 10 -d "$1" "$(cat "$NTFY_URL_FILE")" >/dev/null || true
}
trap 'notify "werft-backup FAILED on $(hostname): line $LINENO"' ERR

mkdir -p "$DUMPS"
if docker compose -f "$COMPOSE" exec -T postgres pg_isready -U werft -d werft >/dev/null 2>&1; then
    docker compose -f "$COMPOSE" exec -T postgres \
        pg_dump -Fc -U werft werft > "$DUMPS/werft-$(date +%F).dump.tmp"
    mv "$DUMPS/werft-$(date +%F).dump.tmp" "$DUMPS/werft-$(date +%F).dump"
    ls -1t "$DUMPS"/werft-*.dump | tail -n +15 | xargs -r rm --   # local dumps: keep 14
else
    notify "werft-backup: postgres down; pushing previous dumps only"
fi

# D9: the GitHub App key is not part of this backup set at all ($DUMPS,
# /opt/werft/compose, /opt/werft/config, /srv/werft/runs — /opt/werft/secrets
# is excluded from the set entirely). The --exclude below is belt-and-braces
# defense-in-depth only, in case /opt/werft/compose or /opt/werft/config ever
# grows a symlink that resolves into /opt/werft/secrets — restic follows
# symlink targets it's told to back up, so an explicit exclude costs nothing
# and prevents a future refactor from silently sweeping the key offsite.
restic backup \
    --exclude /opt/werft/secrets/github_app_key \
    "$DUMPS" /opt/werft/compose /opt/werft/config /srv/werft/runs
restic forget --keep-daily 14 --keep-weekly 8 --prune

# Controller amendment (T8 review): rotate squid's access log after the
# restic push succeeds. USR1 is squid's rotate signal; squid.conf sets
# `logfile_rotate 2`, so this keeps 2 rotated copies on disk. Guarded: a
# stopped/absent egress-proxy container must notify, not fail, the backup —
# log rotation is best-effort ops hygiene, not a backup-correctness concern.
#
# Ordering note: rotating AFTER the backup means the snapshot just pushed
# always contains the pre-rotation (i.e. currently-accumulating) squid log in
# full. The trade-off is the mirror image: if a run's egress activity spans
# the backup window (03:30 by default), a later evidence extraction for that
# run's pre-rotation lines may find them gone from the live log (rotated
# away) even though they were captured in an earlier snapshot. This is
# accepted — see deploy/RUNBOOK.md (Task 16).
if docker kill -s USR1 werft-egress-proxy >/dev/null 2>&1; then
    :
else
    notify "werft-backup: werft-egress-proxy not running; squid log not rotated"
fi
