"""SQLite storage for the ethoscope census.

The census keeps everything it learns in its own SQLite file: one row per
ethoscope database (``db_file``), per metadata sheet (``meta_file``), per
metadata row (``meta_row``) and per row-to-database candidate (``match``).
User data and the results tree are only ever read; this file is the only
thing the census writes.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "3"

# In-place upgrades of older census files: from version -> (to version, statements).
MIGRATIONS = {
    "1": (
        "2",
        (
            "ALTER TABLE meta_file ADD COLUMN date_order TEXT",
            "ALTER TABLE meta_row ADD COLUMN date_alt TEXT",
        ),
    ),
    "2": (
        "3",
        (
            "ALTER TABLE db_file ADD COLUMN experimenter TEXT",
            "ALTER TABLE db_file ADD COLUMN location TEXT",
        ),
    ),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS db_file (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    machine_id TEXT,
    machine_name TEXT,
    path_start TEXT,
    start_utc TEXT,
    size INTEGER,
    mtime REAL,
    sw_version TEXT,
    kernel TEXT,
    camera_sensor TEXT,
    pi_version TEXT,
    stack TEXT,
    tracker TEXT,
    gain TEXT,
    maxfps TEXT,
    n_rois INTEGER,
    roi_ids TEXT,
    duration_h REAL,
    interactor TEXT,
    interactor_kwargs TEXT,
    experimenter TEXT,
    location TEXT,
    readable INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    scanned_at TEXT
);
CREATE INDEX IF NOT EXISTS db_file_machine ON db_file (machine_name);

CREATE TABLE IF NOT EXISTS meta_file (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    sheet TEXT NOT NULL DEFAULT '',
    home TEXT,
    sha256 TEXT,
    size INTEGER,
    mtime REAL,
    n_rows INTEGER,
    columns_raw TEXT,
    column_map TEXT,
    header_row INTEGER,
    date_order TEXT,
    score REAL,
    status TEXT NOT NULL,
    note TEXT,
    UNIQUE (path, sheet)
);

CREATE TABLE IF NOT EXISTS meta_row (
    id INTEGER PRIMARY KEY,
    meta_file_id INTEGER NOT NULL REFERENCES meta_file (id) ON DELETE CASCADE,
    row_idx INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    date TEXT,
    date_status TEXT,
    date_alt TEXT,
    start_time TEXT,
    machine_name TEXT,
    region_id INTEGER,
    sex TEXT,
    genotype_raw TEXT,
    treatment_raw TEXT,
    other_json TEXT
);
CREATE INDEX IF NOT EXISTS meta_row_file ON meta_row (meta_file_id);

CREATE TABLE IF NOT EXISTS match (
    meta_row_id INTEGER NOT NULL REFERENCES meta_row (id) ON DELETE CASCADE,
    db_file_id INTEGER REFERENCES db_file (id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    method TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS match_row ON match (meta_row_id);
CREATE INDEX IF NOT EXISTS match_db ON match (db_file_id);

CREATE TABLE IF NOT EXISTS vocab (
    kind TEXT NOT NULL,
    raw TEXT NOT NULL,
    normalised TEXT,
    curated_by TEXT,
    PRIMARY KEY (kind, raw)
);
"""

DB_FILE_COLUMNS = (
    "path",
    "machine_id",
    "machine_name",
    "path_start",
    "start_utc",
    "size",
    "mtime",
    "sw_version",
    "kernel",
    "camera_sensor",
    "pi_version",
    "stack",
    "tracker",
    "gain",
    "maxfps",
    "n_rois",
    "roi_ids",
    "duration_h",
    "interactor",
    "interactor_kwargs",
    "experimenter",
    "location",
    "readable",
    "error",
    "scanned_at",
)

META_ROW_COLUMNS = (
    "meta_file_id",
    "row_idx",
    "raw_json",
    "date",
    "date_status",
    "date_alt",
    "start_time",
    "machine_name",
    "region_id",
    "sex",
    "genotype_raw",
    "treatment_raw",
    "other_json",
)


class SchemaVersionError(RuntimeError):
    """Raised when a census file was written by an incompatible version."""


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open (creating if needed) a census database.

    Older census files are migrated in place (see :data:`MIGRATIONS`).

    Args:
        path: Location of the census SQLite file.

    Returns:
        A connection with foreign keys enabled and ``sqlite3.Row`` rows.

    Raises:
        SchemaVersionError: If the file holds a schema that cannot be migrated.
    """
    conn = sqlite3.connect(os.fspath(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_info'"
    ).fetchone()
    found = None
    if row is not None:
        version = conn.execute("SELECT value FROM schema_info WHERE key = 'version'").fetchone()
        found = version["value"] if version else None
    while found in MIGRATIONS:
        # Reason: ALTER before SCHEMA, whose CREATE INDEX statements may need the new columns.
        found, statements = MIGRATIONS[found]
        for statement in statements:
            conn.execute(statement)
        conn.execute("UPDATE schema_info SET value = ? WHERE key = 'version'", (found,))
        conn.commit()
    conn.executescript(SCHEMA)
    if found is None:
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES ('version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    elif found != SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionError(
            f"census file {path} has schema {found}, expected {SCHEMA_VERSION}"
        )
    return conn


def upsert_db_file(conn: sqlite3.Connection, record: Mapping[str, Any]) -> int:
    """Insert or replace the ``db_file`` row for ``record['path']``.

    The row id is preserved on update so existing matches stay valid.

    Args:
        conn: Census connection.
        record: Values keyed by ``db_file`` column name; missing keys are NULL.

    Returns:
        The ``db_file.id`` of the row.
    """
    values = [record.get(col) for col in DB_FILE_COLUMNS]
    assignments = ", ".join(f"{col} = excluded.{col}" for col in DB_FILE_COLUMNS[1:])
    conn.execute(
        f"INSERT INTO db_file ({', '.join(DB_FILE_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(DB_FILE_COLUMNS))}) "
        f"ON CONFLICT (path) DO UPDATE SET {assignments}",
        values,
    )
    row = conn.execute("SELECT id FROM db_file WHERE path = ?", (record["path"],)).fetchone()
    return int(row["id"])


def get_db_file(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    """Return the ``db_file`` row for ``path`` or ``None``."""
    return conn.execute("SELECT * FROM db_file WHERE path = ?", (path,)).fetchone()


def meta_files_for_path(conn: sqlite3.Connection, path: str) -> list[sqlite3.Row]:
    """Return all ``meta_file`` rows (one per sheet) registered for ``path``."""
    return conn.execute("SELECT * FROM meta_file WHERE path = ? ORDER BY sheet", (path,)).fetchall()


def delete_meta_path(conn: sqlite3.Connection, path: str) -> None:
    """Remove a metadata file, its rows and their matches from the census."""
    conn.execute("DELETE FROM meta_file WHERE path = ?", (path,))


def insert_meta_file(conn: sqlite3.Connection, record: Mapping[str, Any]) -> int:
    """Insert a ``meta_file`` row and return its id."""
    cols = list(record)
    cur = conn.execute(
        f"INSERT INTO meta_file ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [record[c] for c in cols],
    )
    return int(cur.lastrowid)


def update_meta_file(conn: sqlite3.Connection, meta_file_id: int, **fields: Any) -> None:
    """Update selected columns of one ``meta_file`` row."""
    if not fields:
        return
    assignments = ", ".join(f"{col} = ?" for col in fields)
    conn.execute(
        f"UPDATE meta_file SET {assignments} WHERE id = ?",
        [*fields.values(), meta_file_id],
    )


def replace_meta_rows(
    conn: sqlite3.Connection, meta_file_id: int, rows: Iterable[Mapping[str, Any]]
) -> int:
    """Replace all rows of a metadata sheet (and drop their matches).

    Args:
        conn: Census connection.
        meta_file_id: The sheet the rows belong to.
        rows: Row records keyed by ``meta_row`` column name.

    Returns:
        The number of rows inserted.
    """
    conn.execute("DELETE FROM meta_row WHERE meta_file_id = ?", (meta_file_id,))
    cols = META_ROW_COLUMNS
    sql = f"INSERT INTO meta_row ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    data = [[meta_file_id if c == "meta_file_id" else r.get(c) for c in cols] for r in rows]
    conn.executemany(sql, data)
    return len(data)
