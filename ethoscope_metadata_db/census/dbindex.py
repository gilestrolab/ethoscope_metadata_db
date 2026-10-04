"""Index the ethoscope results tree.

Walks a results root laid out as::

    <machine_id>/<machine_name>/<YYYY-MM-DD_HH-MM-SS>/<YYYY-MM-DD_HH-MM-SS>_<machine_id>.db

and records, for each ``.db``, what its path says and what its ``METADATA``
table says. Databases are opened strictly read-only with
``mode=ro&immutable=1``. Because ``immutable=1`` ignores a write-ahead log, a
database whose ``-wal`` sidecar is non-empty would be read without its newest
rows: such files are recorded as unreadable rather than read inaccurately.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterator

from . import store
from .normalise import kernel_stack

log = logging.getLogger(__name__)

_STAMP_FMT = "%Y-%m-%d_%H-%M-%S"
_STAMP_RE = r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}"
_DB_NAME_RE = re.compile(rf"^({_STAMP_RE})_(\w+)\.db$")
_ROI_TABLE_RE = re.compile(r"^ROI_(\d+)$")


@dataclass(frozen=True)
class PathInfo:
    """What the results-tree path of a database tells us.

    Attributes:
        machine_id: Directory-level machine id (normally a 32-char hex string).
        machine_name: Directory-level machine name, e.g. ``ETHOSCOPE_007``.
        start: Start timestamp from the experiment directory (naive).
        conforms: Whether the path follows the standard layout.
    """

    machine_id: str | None
    machine_name: str | None
    start: dt.datetime | None
    conforms: bool


def parse_results_path(rel_path: str) -> PathInfo:
    """Parse a database path relative to the results root.

    Only the last four components are used, so absolute paths work too.

    Args:
        rel_path: e.g. ``abc.../ETHOSCOPE_007/2024-01-05_10-00-00/2024-01-05_10-00-00_abc....db``.

    Returns:
        A :class:`PathInfo`; ``conforms`` is False when the layout differs, in
        which case only fields that could still be read are filled in.
    """
    parts = rel_path.replace(os.sep, "/").split("/")
    name_match = _DB_NAME_RE.match(parts[-1])
    start = None
    if name_match:
        start = dt.datetime.strptime(name_match.group(1), _STAMP_FMT)
    if len(parts) < 4 or not name_match:
        return PathInfo(None, None, start, False)
    machine_id, machine_name, stamp_dir = parts[-4:-1]
    conforms = stamp_dir == name_match.group(1) and machine_id == name_match.group(2)
    return PathInfo(machine_id, machine_name, start, conforms)


def iter_db_files(results_root: str) -> Iterator[str]:
    """Yield absolute paths of all ``*.db`` files below ``results_root``, sorted."""
    for root, dirnames, filenames in os.walk(results_root):
        dirnames.sort()
        for filename in sorted(filenames):
            if filename.endswith(".db"):
                yield os.path.join(root, filename)


def wal_size(db_path: str) -> int:
    """Return the size of the ``-wal`` sidecar of ``db_path`` (0 if absent)."""
    try:
        return os.path.getsize(db_path + "-wal")
    except OSError:
        return 0


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open an ethoscope database strictly read-only and probe it.

    Args:
        db_path: Absolute path of an existing ``.db`` file.

    Returns:
        A connection that has already executed a statement successfully.

    Raises:
        FileNotFoundError: If the file does not exist (``immutable=1`` would
            otherwise silently create an empty database view).
        sqlite3.DatabaseError: If SQLite cannot read the file.
    """
    if not os.path.isfile(db_path):
        raise FileNotFoundError(db_path)
    uri = f"file:{urllib.parse.quote(os.path.abspath(db_path))}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn


def literal_or_text(text: str | None) -> Any:
    """Return ``ast.literal_eval(text)`` or the original text if that fails."""
    if text is None:
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return text


def _class_name(value: Any) -> str | None:
    """Reduce ``"<class 'a.b.Tracker'>"`` or ``'a.b.Tracker'`` to ``Tracker``."""
    if value is None:
        return None
    text = str(value)
    match = re.search(r"<class '([\w.]+)'>", text)
    if match:
        text = match.group(1)
    return text.rsplit(".", 1)[-1].strip() or None


def _option_name(options: Any, key: str) -> tuple[str | None, Any]:
    """Extract ``(class name, kwargs)`` for ``key`` from ``selected_options``."""
    if isinstance(options, dict):
        entry = options.get(key)
        if isinstance(entry, dict):
            return _class_name(entry.get("name") or entry.get("class")), entry.get("kwargs")
        return _class_name(entry), None
    if isinstance(options, str):
        # Not a valid literal (e.g. contains <class ...> reprs): use a regex.
        pattern = rf"['\"]{key}['\"]\s*:\s*\{{.*?['\"](?:name|class)['\"]\s*:\s*(<class '[\w.]+'>|['\"][\w.]+['\"])"
        match = re.search(pattern, options, re.S)
        if match:
            return _class_name(match.group(1).strip("'\"")), None
    return None, None


def _camera_sensor(camera: Any) -> str | None:
    if isinstance(camera, dict):
        for key in ("sensor", "model", "name", "type"):
            if camera.get(key):
                return str(camera[key])
        return json.dumps(camera, sort_keys=True, default=str) if camera else None
    return str(camera) if camera not in (None, "") else None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    try:
        moment = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.replace(tzinfo=None).isoformat(timespec="seconds")


def read_db_metadata(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read the facts the census needs from an open ethoscope database.

    Args:
        conn: A read-only connection from :func:`open_readonly`.

    Returns:
        A partial ``db_file`` record (no path, size or mtime).
    """
    meta = dict(conn.execute("SELECT field, value FROM METADATA").fetchall())
    hardware = literal_or_text(meta.get("hardware_info"))
    if not isinstance(hardware, dict):
        hardware = {}
    options = literal_or_text(meta.get("selected_options"))
    tracker, _ = _option_name(options, "tracker")
    interactor, interactor_kwargs = _option_name(options, "interactor")

    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    roi_ids: list[int] = []
    if "ROI_MAP" in tables:
        roi_ids = sorted(int(r[0]) for r in conn.execute("SELECT roi_idx FROM ROI_MAP"))

    start = _float(meta.get("date_time"))
    stop = _float(meta.get("stop_date_time"))
    duration_h = (stop - start) / 3600 if start is not None and stop is not None else None
    if duration_h is None:
        last_t = 0
        for table in tables:
            if _ROI_TABLE_RE.match(table):
                # rowid order follows insertion; avoids a full scan for MAX(t).
                row = conn.execute(
                    f'SELECT t FROM "{table}" ORDER BY rowid DESC LIMIT 1'
                ).fetchone()
                if row and row[0] is not None:
                    last_t = max(last_t, float(row[0]))
        duration_h = last_t / 3.6e6 if last_t else None

    kernel = hardware.get("kernel")
    return {
        "machine_id": meta.get("machine_id"),
        "machine_name": meta.get("machine_name"),
        "start_utc": _utc_iso(start),
        "sw_version": meta.get("version"),
        "kernel": kernel,
        "camera_sensor": _camera_sensor(hardware.get("camera")),
        "pi_version": hardware.get("pi_version"),
        "stack": kernel_stack(kernel),
        "tracker": tracker or _class_name(meta.get("tracker_class")),
        "gain": meta.get("gain_setting"),
        "maxfps": meta.get("maxfps_setting"),
        "n_rois": len(roi_ids),
        "roi_ids": json.dumps(roi_ids),
        "duration_h": round(duration_h, 3) if duration_h is not None else None,
        "interactor": interactor,
        "interactor_kwargs": (
            json.dumps(interactor_kwargs, sort_keys=True, default=str)
            if interactor_kwargs
            else None
        ),
    }


def index_db(db_path: str, results_root: str) -> dict[str, Any]:
    """Build the ``db_file`` record for one database. Never raises on bad files.

    Args:
        db_path: Absolute path of the ``.db`` file.
        results_root: Root of the results tree (for path parsing).

    Returns:
        A full ``db_file`` record; ``readable`` is 0 and ``error`` says why
        when the file could not be read.
    """
    stat = os.stat(db_path)
    info = parse_results_path(os.path.relpath(db_path, results_root))
    record: dict[str, Any] = {
        "path": os.path.abspath(db_path),
        "machine_id": info.machine_id,
        "machine_name": info.machine_name,
        "path_start": info.start.isoformat(timespec="seconds") if info.start else None,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "readable": 0,
        "error": None if info.conforms else "non-standard path",
        "scanned_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    pending_wal = wal_size(db_path)
    if pending_wal:
        record["error"] = f"non-empty WAL ({pending_wal} bytes): skipped"
        return record
    try:
        conn = open_readonly(db_path)
        try:
            found = read_db_metadata(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    # The path is authoritative for machine identity in the tree; METADATA
    # values only fill gaps (and disagreements are noted).
    for key in ("machine_id", "machine_name"):
        if record[key] is None:
            record[key] = found[key]
        elif found[key] and found[key] != record[key]:
            note = f"{key} differs in METADATA: {found[key]}"
            record["error"] = f"{record['error']}; {note}" if record["error"] else note
        found.pop(key)
    record.update(found)
    record["readable"] = 1
    return record


def scan_results(
    conn: sqlite3.Connection, results_root: str, rescan: bool = False
) -> dict[str, int]:
    """Index every database under ``results_root`` into the census.

    Files whose size and mtime are unchanged since the last scan are skipped
    unless ``rescan`` is True.

    Args:
        conn: Census connection.
        results_root: Root of the results tree.
        rescan: Re-read every file even if unchanged.

    Returns:
        Counts: ``seen``, ``indexed``, ``unchanged``, ``unreadable``.
    """
    counts = {"seen": 0, "indexed": 0, "unchanged": 0, "unreadable": 0}
    for db_path in iter_db_files(results_root):
        counts["seen"] += 1
        path = os.path.abspath(db_path)
        previous = store.get_db_file(conn, path)
        try:
            stat = os.stat(db_path)
        except OSError as exc:
            log.warning("cannot stat %s: %s", db_path, exc)
            continue
        unchanged = (
            previous is not None
            and previous["size"] == stat.st_size
            and previous["mtime"] == stat.st_mtime
            and previous["readable"]
        )
        if unchanged and not rescan:
            counts["unchanged"] += 1
            continue
        record = index_db(db_path, results_root)
        store.upsert_db_file(conn, record)
        counts["indexed"] += 1
        counts["unreadable"] += 0 if record["readable"] else 1
        if counts["indexed"] % 500 == 0:
            conn.commit()
    conn.commit()
    return counts
