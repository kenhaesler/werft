#!/usr/bin/env bash
# Nightly Werft backup (SPEC §2/§10; lineage §10.6): pg_dump -Fc + restic offsite.
# Retention 14 daily + 8 weekly. D9: the GitHub App key is EXCLUDED (re-creatable).
#
# Host-side, run as root by werft-backup.service/.timer. Must work when the
# manager container is broken or the whole stack is down: only postgres needs
# to be up for the dump half, and restic still pushes whatever dumps already
# exist on disk even if pg_isready never succeeds this run.
# -E: without it bash does not inherit the ERR trap into functions/subshells, so
# a failure inside one would notify nothing.
set -Eeuo pipefail
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
    # Computed once: date +%F evaluated separately at redirect time and mv
    # time could straddle midnight and orphan a .tmp file that neither the
    # retention glob nor the drill's newest-dump glob would ever see.
    STAMP=$(date +%F)
    docker compose -f "$COMPOSE" exec -T postgres \
        pg_dump -Fc -U werft werft > "$DUMPS/werft-$STAMP.dump.tmp"
    mv "$DUMPS/werft-$STAMP.dump.tmp" "$DUMPS/werft-$STAMP.dump"
    # SC2012: `ls -t` is deliberate — newest-first by mtime is the ordering
    # retention needs, and these filenames are ours (`werft-YYYY-MM-DD.dump`),
    # never user-supplied, so the non-alphanumeric hazard does not apply.
    # shellcheck disable=SC2012
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
# D9 amendment (T9 final review): a *live* run's tree carries plaintext
# credentials — `runs/<id>/secrets/` holds the GitHub installation token and the
# CLAUDE_CODE_OAUTH_TOKEN, and its `task.json` still carries them in `env` until
# the teardown scrub rewrites it. The nightly timer fires at 03:30 regardless of
# what is mid-flight, so without these excludes every live run's credentials go
# offsite in cleartext. `task.json` is reconstructible from the row and, once
# scrubbed, carries no evidence value the collected artifacts do not already
# hold — so excluding it unconditionally costs nothing.
restic backup \
    --exclude /opt/werft/secrets/github_app_key \
    --exclude '/srv/werft/runs/*/secrets' \
    --exclude '/srv/werft/runs/*/task.json' \
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

# Guard the script's exit status against any future appends after this point
# (an appended non-critical step must not accidentally fail the whole run
# via its own trailing command's status) — explicit success here.
exit 0
