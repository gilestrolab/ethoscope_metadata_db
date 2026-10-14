# Census site: the nightly job

`nightly.sh` refreshes the census, then backs up the site:

| step | what | kept |
|---|---|---|
| refresh | `ethocensus refresh --config census.toml`: scan, discover, ingest, match, summarise | |
| curation | labels, projects, people, notes and the edit log (`backup_sqlite.sh`) | newest 14 |
| census-db | the census database, zstd-compressed, only when it was summarised again | newest 3 |
| metadata-files | every accepted metadata file, stored by content (`ethocensus archive-metadata`), with a catalogue of every path and version | all, never deleted |

Each step runs even if an earlier one failed, and the job fails if any did. With
`GARAGE_BUCKET` set, every backup is also copied to a Garage (S3) bucket through the rclone
Docker image (`garage.sh`); the curation and census copies rotate there as they do locally,
and the metadata archive is only ever added to. The web UI notices a refreshed census on its
next request and rebuilds owners and labels for it; it needs no restart.

## Install (systemd user service)

```
mkdir -p ~/.config/ethocensus ~/.config/credstore.encrypted
cp deploy/nightly.env.example ~/.config/ethocensus/nightly.env    # fill in; no secrets
# the Garage key, as GARAGE_KEY=... and GARAGE_SECRET=... lines, encrypted at rest:
systemd-creds --user encrypt --name=garage garage.env ~/.config/credstore.encrypted/ethocensus-garage
rm garage.env
systemctl --user link "$PWD/deploy/systemd/census-nightly.service" "$PWD/deploy/systemd/census-nightly.timer"
systemctl --user enable --now census-nightly.timer
loginctl enable-linger "$USER"    # so the timer runs without a login session
```

The service decrypts the key only for itself (`LoadCredentialEncrypted=`); the encrypted file
is bound to this host and user. Logs: `journalctl --user -t census-nightly`. Run it now with
`systemctl --user start census-nightly.service`.
