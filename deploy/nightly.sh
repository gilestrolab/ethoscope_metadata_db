#!/usr/bin/env bash
# The census site's nightly job: refresh the census, then back everything up.
#
#   refresh         ethocensus refresh (scan, discover, ingest, match, summarise); the web UI
#                   rebuilds owners and labels for the new summary on its next request
#   curation        labels, projects, people, notes, edit log: every night, newest 14 kept
#   census-db       the census database, compressed, only when re-summarised: newest 3 kept
#   metadata-files  every accepted metadata file by content (ethocensus archive-metadata):
#                   only new contents are copied, nothing is ever deleted
#
# Backups go to BACKUP_DIR and, with GARAGE_BUCKET set, offsite to Garage (S3). Every step
# runs even if an earlier one failed (a failed refresh leaves yesterday's census to back up);
# the job fails if any step did.
#
# Settings, from the environment (see systemd/census-nightly.service and nightly.env.example):
#   CENSUS_DIR       holds census.sqlite and curation.sqlite
#   CENSUS_CONFIG    census.toml for the refresh (no refresh when unset)
#   BACKUP_DIR       local backup root, one folder per part
#   ETHOCENSUS       the ethocensus command of the environment the census runs in
#   GARAGE_BUCKET    offsite bucket (no offsite copies when unset)
#   GARAGE_ENV_FILE  file with GARAGE_KEY and GARAGE_SECRET; by default the systemd
#                    credential "garage" ($CREDENTIALS_DIRECTORY/garage)
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
: "${CENSUS_DIR:?}" "${BACKUP_DIR:?}" "${ETHOCENSUS:?}"
if [ -z "${GARAGE_ENV_FILE:-}" ] && [ -n "${CREDENTIALS_DIRECTORY:-}" ]; then
    export GARAGE_ENV_FILE="$CREDENTIALS_DIRECTORY/garage"
fi
census="$CENSUS_DIR/census.sqlite"
offsite() { [ -z "${GARAGE_BUCKET:-}" ] || echo "$GARAGE_BUCKET/$1"; }
failed=()

if [ -n "${CENSUS_CONFIG:-}" ]; then
    echo "== refresh"
    "$ETHOCENSUS" --census "$census" refresh --config "$CENSUS_CONFIG" || failed+=(refresh)
fi

echo "== curation"
GARAGE_REMOTE=$(offsite census/curation) \
    "$here/backup_sqlite.sh" "$CENSUS_DIR/curation.sqlite" "$BACKUP_DIR/curation" 14 ||
    failed+=(curation)

echo "== census-db"
COMPRESS=zstd STAMP_SQL="SELECT value FROM schema_info WHERE key = 'summarised_at'" \
    GARAGE_REMOTE=$(offsite census/census-db) \
    "$here/backup_sqlite.sh" "$census" "$BACKUP_DIR/census-db" 3 ||
    failed+=(census-db)

echo "== metadata-files"
archive="$BACKUP_DIR/metadata-files"
if "$ETHOCENSUS" --census "$census" archive-metadata "$archive"; then
    if [ -n "${GARAGE_BUCKET:-}" ]; then
        # Reason: copy, never sync: a file deleted here must stay in the offsite archive.
        (
            source "$here/garage.sh"
            garage_setup "${GARAGE_ENV_FILE:?file with GARAGE_KEY and GARAGE_SECRET}" "$archive"
            garage_rclone copy "$archive" "garage:$GARAGE_BUCKET/metadata-files"
        ) && echo "copied offsite -> garage:$GARAGE_BUCKET/metadata-files" ||
            failed+=(metadata-files-offsite)
    fi
else
    failed+=(metadata-files)
fi

if ((${#failed[@]})); then
    echo "nightly job failed: ${failed[*]}" >&2
    exit 1
fi
echo "nightly job done"
