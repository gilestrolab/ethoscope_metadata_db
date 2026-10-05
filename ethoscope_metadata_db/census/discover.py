"""Find ethoscope metadata files in user homes and register them.

Each tabular file is read and one ``meta_file`` row per sheet is stored
(``candidate`` if its first row has every column ethoscopy requires,
``rejected`` otherwise; ``score`` is the fraction of those columns present),
with its provenance (path, home, sheet, size, mtime, SHA-256). Re-running is
cheap: files with unchanged size and mtime are skipped, and files whose
content hash is unchanged keep their rows.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Iterable, Iterator

from . import store
from .ingest import (
    find_header,
    header_names,
    ingest_sheet,
    is_metadata,
    map_columns,
    read_tabular,
    score_columns,
    sha256_file,
)

log = logging.getLogger(__name__)

# .txt included: rethomics-era metadata was often saved with R's write.table.
DEFAULT_EXTENSIONS = frozenset({".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"})
DEFAULT_MAX_MB = 20.0
SKIP_DIRS = frozenset({"__pycache__", "site-packages", "node_modules", "venv", "env"})


def _walk(root: str, exclude: Iterable[str]) -> Iterator[str]:
    skip = SKIP_DIRS | set(exclude)
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in skip)
        for filename in sorted(filenames):
            yield os.path.join(current, filename)


def iter_tabular_files(
    root: str,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    max_bytes: int | None = None,
    exclude: Iterable[str] = (),
    paths: Iterable[str] | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield ``(path, home)`` for tabular files under a homes root.

    Hidden files and directories, virtualenvs and package trees are skipped,
    and symlinked directories are not followed. ``home`` is the first path
    component below ``root`` (``""`` for files directly in ``root``).

    Args:
        root: Directory containing one sub-directory per user home.
        extensions: Lower-case extensions to accept, with the dot.
        max_bytes: Skip files larger than this.
        exclude: Further directory names to skip at any depth (e.g. ``secrets``).
        paths: Only these files (absolute, or relative to ``root``) instead of
            walking the tree, e.g. what a backup holds that the homes no longer do.
    """
    wanted = {e.lower() for e in extensions}
    if paths is None:
        candidates = _walk(root, exclude)
    else:
        candidates = iter(sorted(os.path.join(root, p) for p in paths))
    for path in candidates:
        filename = os.path.basename(path)
        if filename.startswith((".", "~$")):
            continue
        if os.path.splitext(filename)[1].lower() not in wanted:
            continue
        try:
            size = os.path.getsize(path)
        except OSError as exc:  # broken symlink, vanished or unreadable file
            log.warning("cannot stat %s: %s", path, exc)
            continue
        if max_bytes is not None and size > max_bytes:
            log.info("skipping large file %s", path)
            continue
        rel = os.path.relpath(path, root).split(os.sep)
        yield os.path.abspath(path), rel[0] if len(rel) > 1 else ""


def register_file(
    conn: sqlite3.Connection,
    path: str,
    home: str | None,
    force: bool = False,
) -> str:
    """Register (or refresh) one tabular file as ``meta_file`` rows.

    Args:
        conn: Census connection.
        path: Absolute path of the file.
        home: The owner's home name, for provenance.
        force: Re-read the file even if size, mtime and hash are unchanged.

    Returns:
        ``"unchanged"``, ``"touched"`` (same hash, new mtime), ``"registered"``
        or ``"unreadable"``.
    """
    stat = os.stat(path)
    existing = store.meta_files_for_path(conn, path)
    if existing and not force:
        first = existing[0]
        if first["size"] == stat.st_size and first["mtime"] == stat.st_mtime:
            return "unchanged"
    digest = sha256_file(path)
    if existing and not force and existing[0]["sha256"] == digest:
        conn.execute("UPDATE meta_file SET mtime = ? WHERE path = ?", (stat.st_mtime, path))
        return "touched"

    store.delete_meta_path(conn, path)
    provenance = {
        "path": path,
        "home": home,
        "sha256": digest,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }
    try:
        sheets = read_tabular(path)
    except Exception as exc:  # noqa: BLE001 - any reader failure means unreadable
        store.insert_meta_file(
            conn, {**provenance, "sheet": "", "status": "unreadable", "note": f"{exc}"[:500]}
        )
        return "unreadable"
    for sheet, frame in sheets:
        _, mapping, _ = find_header(frame)
        # The header of every sheet, accepted or not, shows which column names are in use.
        header = header_names(list(frame.iloc[0])) if not frame.empty else []
        store.insert_meta_file(
            conn,
            {
                **provenance,
                "sheet": sheet,
                "columns_raw": json.dumps(header, ensure_ascii=False) if header else None,
                "score": score_columns(mapping),
                "status": "candidate" if is_metadata(mapping) else "rejected",
            },
        )
    return "registered"


def discover(
    conn: sqlite3.Connection,
    homes_root: str,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    max_mb: float | None = DEFAULT_MAX_MB,
    exclude: Iterable[str] = (),
    paths: Iterable[str] | None = None,
) -> dict[str, int]:
    """Walk ``homes_root`` and register every tabular file found.

    Args:
        conn: Census connection.
        homes_root: Directory holding the user homes.
        extensions: File extensions to consider.
        max_mb: Skip files larger than this many megabytes (None: no limit).
        exclude: Directory names to skip (see :func:`iter_tabular_files`).
        paths: Register only these files (see :func:`iter_tabular_files`).

    Returns:
        Counts per :func:`register_file` outcome plus ``seen`` and ``bad_name``
        (files skipped because their name is not valid UTF-8).
    """
    max_bytes = int(max_mb * 1024 * 1024) if max_mb is not None else None
    counts = {"seen": 0, "unchanged": 0, "touched": 0, "registered": 0, "unreadable": 0}
    counts["bad_name"] = 0
    for path, home in iter_tabular_files(homes_root, extensions, max_bytes, exclude, paths):
        counts["seen"] += 1
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            # Reason: a name that is not valid UTF-8 cannot be stored as SQLite text.
            log.warning("skipping file whose name is not UTF-8: %r", path)
            counts["bad_name"] += 1
            continue
        try:
            outcome = register_file(conn, path, home)
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
            continue
        counts[outcome] += 1
        if counts["seen"] % 200 == 0:
            conn.commit()
    conn.commit()
    return counts


def ingest_candidates(
    conn: sqlite3.Connection, paths: Iterable[str] | None = None, reparse: bool = False
) -> dict[str, int]:
    """Parse rows for all ``candidate`` sheets (optionally only some paths).

    Args:
        conn: Census connection.
        paths: Restrict to these absolute paths.
        reparse: Also re-parse sheets already ingested, e.g. after a change
            to the parsing rules (their matches are dropped with the old rows),
            and re-check rejected sheets' stored headers against the rules.

    Returns:
        Counts: ``files``, ``sheets``, ``rows``, ``failed``.
    """
    if reparse:
        conn.execute(
            "UPDATE meta_file SET status = 'candidate' WHERE status IN ('ingested', 'empty')"
        )
        rejected = conn.execute(
            "SELECT id, columns_raw FROM meta_file "
            "WHERE status IN ('rejected', 'no_key_columns') AND columns_raw IS NOT NULL"
        ).fetchall()
        for row in rejected:
            mapping, _ = map_columns(json.loads(row["columns_raw"]))
            if is_metadata(mapping):
                store.update_meta_file(
                    conn, row["id"], status="candidate", score=score_columns(mapping), note=None
                )
    sql = "SELECT id, path, sheet FROM meta_file WHERE status = 'candidate'"
    pending = conn.execute(sql).fetchall()
    if paths is not None:
        wanted = {os.path.abspath(p) for p in paths}
        pending = [r for r in pending if r["path"] in wanted]
    by_path: dict[str, dict[str, int]] = {}
    for row in pending:
        by_path.setdefault(row["path"], {})[row["sheet"]] = row["id"]

    counts = {"files": 0, "sheets": 0, "rows": 0, "failed": 0}
    for path, sheet_ids in by_path.items():
        counts["files"] += 1
        try:
            sheets = dict(read_tabular(path))
        except Exception as exc:  # noqa: BLE001 - file changed or vanished since discovery
            for meta_file_id in sheet_ids.values():
                store.update_meta_file(conn, meta_file_id, status="unreadable", note=str(exc)[:500])
            counts["failed"] += 1
            continue
        for sheet, meta_file_id in sheet_ids.items():
            if sheet not in sheets:
                store.update_meta_file(
                    conn, meta_file_id, status="unreadable", note="sheet vanished"
                )
                counts["failed"] += 1
                continue
            counts["sheets"] += 1
            counts["rows"] += ingest_sheet(conn, meta_file_id, sheets[sheet])
        conn.commit()
    return counts


def ingest_files(
    conn: sqlite3.Connection, paths: Iterable[str], home: str | None = None, force: bool = False
) -> dict[str, int]:
    """Register and ingest explicitly named files.

    Sheets lacking a required column end up ``no_key_columns`` with a note
    naming the missing column(s).

    Args:
        conn: Census connection.
        paths: Files to ingest.
        home: Provenance label for these files.
        force: Re-read even if unchanged.

    Returns:
        Counts as in :func:`ingest_candidates`.
    """
    absolute = [os.path.abspath(p) for p in paths]
    for path in absolute:
        register_file(conn, path, home, force=force)
        # Parse named files even if rejected, so the census records which
        # required column is missing.
        conn.execute(
            "UPDATE meta_file SET status = 'candidate' WHERE path = ? AND status = 'rejected'",
            (path,),
        )
    conn.commit()
    return ingest_candidates(conn, absolute)
