# Shared helpers for copying backups to a Garage (S3) bucket with the rclone Docker image.
# Source this file, call garage_setup, then garage_rclone / garage_keep_last.
#   GARAGE_ENDPOINT  S3 endpoint (default http://127.0.0.1:3900)
#   RCLONE_IMAGE     rclone Docker image (default rclone/rclone:1.75.2)

# garage_setup ENV_FILE LOCAL_ROOT
#   ENV_FILE holds GARAGE_KEY (key id) and GARAGE_SECRET (keep it mode 600, out of git);
#   LOCAL_ROOT is the folder rclone may read (mounted read-only at the same path).
garage_setup() {
    local env_file=${1:?file with GARAGE_KEY and GARAGE_SECRET}
    GARAGE_LOCAL_ROOT=${2:?local folder rclone may read}
    GARAGE_CONF=$(mktemp)
    trap 'rm -f "$GARAGE_CONF"' EXIT
    chmod 600 "$GARAGE_CONF"
    # Reason: credentials go in through an env file, never on the command line (visible in ps).
    {
        echo "RCLONE_CONFIG_GARAGE_TYPE=s3"
        echo "RCLONE_CONFIG_GARAGE_PROVIDER=Other"
        echo "RCLONE_CONFIG_GARAGE_ENDPOINT=${GARAGE_ENDPOINT:-http://127.0.0.1:3900}"
        echo "RCLONE_CONFIG_GARAGE_REGION=garage"
        echo "RCLONE_CONFIG_GARAGE_FORCE_PATH_STYLE=true"
        echo "RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID=$(sed -n 's/^GARAGE_KEY=//p' "$env_file")"
        echo "RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY=$(sed -n 's/^GARAGE_SECRET=//p' "$env_file")"
    } > "$GARAGE_CONF"
}

garage_rclone() {
    docker run --rm --network host --env-file "$GARAGE_CONF" \
        -v "$GARAGE_LOCAL_ROOT":"$GARAGE_LOCAL_ROOT":ro \
        "${RCLONE_IMAGE:-rclone/rclone:1.75.2}" --quiet "$@"
}

# garage_keep_last N REMOTE PREFIX: delete all but the newest N objects named PREFIX* in
# bucket/folder REMOTE (backup names carry a timestamp, so they sort by date).
garage_keep_last() {
    local keep=$1 remote=$2 prefix=$3
    garage_rclone lsf --files-only "garage:$remote" | { grep "^$prefix" || true; } | sort | head -n -"$keep" |
        while read -r name; do garage_rclone deletefile "garage:$remote/$name"; done
}
