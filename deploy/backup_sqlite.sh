#!/usr/bin/env bash
# Back up a live SQLite file safely (online .backup), check it, keep the newest N copies,
# and optionally copy each one offsite to a Garage (S3) bucket, keeping the newest N there too.
#
# Usage: backup_sqlite.sh SOURCE.sqlite BACKUP_DIR [KEEP=14]
#
#   COMPRESS=zstd     compress each copy, after its integrity check
#   STAMP_SQL=...     a query on SOURCE; skip the backup when its result is the same as at the
#                     last backup (e.g. the census's summarised_at: back up only when it changed)
#   GARAGE_REMOTE     bucket/folder for the offsite copy (no offsite copy when unset)
#   GARAGE_ENV_FILE   file holding GARAGE_KEY and GARAGE_SECRET (see garage.sh)
set -euo pipefail
here=$(dirname "$(readlink -f "$0")")
src=${1:?SQLite file to back up}
dest=${2:?backup folder}
keep=${3:-14}
mkdir -p "$dest"
chmod 700 "$dest"
prefix="$(basename "$src" .sqlite)_nightly_"
stamp_file="$dest/.${prefix}stamp"
stamp=""
if [ -n "${STAMP_SQL:-}" ]; then
    stamp=$(sqlite3 -readonly "$src" "$STAMP_SQL")
    if [ -f "$stamp_file" ] && [ "$(cat "$stamp_file")" = "$stamp" ]; then
        echo "unchanged since the last backup ($stamp): $src"
        exit 0
    fi
fi
name="${prefix}$(date +%Y%m%d_%H%M%S).sqlite"
out="$dest/$name"
sqlite3 "$src" ".backup '$out'"
chmod 600 "$out"
if [ "$(sqlite3 "$out" 'PRAGMA integrity_check')" != "ok" ]; then
    echo "backup failed its integrity check: $out" >&2
    exit 1
fi
if [ "${COMPRESS:-}" = "zstd" ]; then
    zstd -q -T0 --rm "$out" -o "$out.zst"
    out="$out.zst"
    name="$name.zst"
fi
# Reason: only this script's own copies rotate; hand-made backups in the folder are kept.
ls -1t "$dest/$prefix"* | tail -n +$((keep + 1)) | xargs -r rm --
echo "backed up $src -> $out"

if [ -n "${GARAGE_REMOTE:-}" ]; then
    # shellcheck source=garage.sh
    source "$here/garage.sh"
    garage_setup "${GARAGE_ENV_FILE:?file with GARAGE_KEY and GARAGE_SECRET}" "$dest"
    garage_rclone copyto "$out" "garage:$GARAGE_REMOTE/$name"
    garage_keep_last "$keep" "$GARAGE_REMOTE" "$prefix"
    echo "copied offsite -> garage:$GARAGE_REMOTE/$name"
fi
[ -z "$stamp" ] || echo "$stamp" > "$stamp_file"
