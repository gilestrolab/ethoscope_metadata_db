"""A content-addressed archive of every accepted metadata file: a backup of the lab's metadata.

Metadata files live in people's homes and archives, where they get edited, moved and
deleted. The archive keeps one copy of each distinct content, ``files/<ab>/<sha256><ext>``,
and a catalogue (``catalogue.csv``) of every (sha256, path) pair ever seen, with the date it
was first seen, so any version of any sheet can be found again from its path. Nothing is
ever deleted, and a run only copies what is new, so the archive can be synced offsite
incrementally.

Files are hashed when they are copied, not taken from the census, so a sheet edited after
the last census scan is archived as it is now.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

CATALOGUE_FIELDS = ("sha256", "path", "size", "mtime", "first_seen")
ARCHIVED_STATUSES = ("ingested",)  # metadata files the census accepted


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stored_path(archive: Path, sha256: str, source: str) -> Path:
    """Where a file with this content lives in the archive (keeps the original extension)."""
    return archive / "files" / sha256[:2] / f"{sha256}{Path(source).suffix.lower()}"


def _catalogue(archive: Path) -> set[tuple[str, str]]:
    path = archive / "catalogue.csv"
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8", errors="surrogateescape") as fh:
        return {(row["sha256"], row["path"]) for row in csv.DictReader(fh)}


def archive_metadata(
    conn: sqlite3.Connection,
    archive_dir: str | os.PathLike[str],
    statuses: tuple[str, ...] = ARCHIVED_STATUSES,
    today: dt.date | None = None,
) -> dict[str, int]:
    """Copy every accepted metadata file not yet archived and extend the catalogue.

    Args:
        conn: Census connection (only read).
        archive_dir: The archive folder; created if missing.
        statuses: ``meta_file`` statuses whose files are archived.
        today: Date written as ``first_seen`` (default: today).

    Returns:
        Counts: files considered, new contents copied, new catalogue entries, files missing
        or unreadable, and distinct contents in the archive.
    """
    archive = Path(archive_dir)
    (archive / "files").mkdir(parents=True, exist_ok=True)
    seen = _catalogue(archive)
    first_seen = (today or dt.date.today()).isoformat()
    paths = [
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT path FROM meta_file WHERE status IN ({', '.join('?' * len(statuses))}) "
            "ORDER BY path",
            statuses,
        )
    ]
    counts = {"files": len(paths), "copied": 0, "new_entries": 0, "missing": 0}
    new_rows = []
    for source in paths:
        try:
            info = os.stat(source)
            sha = file_sha256(source)
        except OSError:
            counts["missing"] += 1
            continue
        target = stored_path(archive, sha, source)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            shutil.copyfile(source, partial)
            # Reason: a file changed while being copied must not be stored under the old hash.
            if file_sha256(partial) != sha:
                partial.unlink()
                counts["missing"] += 1
                continue
            os.replace(partial, target)
            counts["copied"] += 1
        if (sha, source) not in seen:
            seen.add((sha, source))
            new_rows.append(
                {
                    "sha256": sha,
                    "path": source,
                    "size": info.st_size,
                    "mtime": dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "first_seen": first_seen,
                }
            )
    if new_rows:
        path = archive / "catalogue.csv"
        is_new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8", errors="surrogateescape") as fh:
            writer = csv.DictWriter(fh, fieldnames=CATALOGUE_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerows(new_rows)
    counts["new_entries"] = len(new_rows)
    counts["contents"] = sum(1 for p in (archive / "files").rglob("*") if p.is_file())
    return counts
