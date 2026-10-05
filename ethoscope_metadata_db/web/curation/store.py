"""Human curation, kept apart from the machine-built census.

``curation.sqlite`` holds what people decide: labels (free tags, or projects
with a DOI and description) linked to metadata files, runs or whole folders;
people and the aliases that name them (home folders, experimenter strings
typed on the nodes); notes; and an audit log. Labels and aliases are keyed by
path or string, never by census id, because census ids change when files are
re-registered: the census can be rebuilt without losing any curation.

A person has a full ``name``, an optional lab ``username`` (unique) and
``role``, and is ``active`` (in the lab now) or not (alumni, the default).
Their ``kind`` is ``person`` (a real, unambiguous person: only these own runs
and files) or ``shared`` (a name reviewed and found not to be a person: guest
accounts, shared or project folders, teams).

Every edit goes through :class:`Change`, which records the before and after
of each row it touches, so :func:`undo` can reverse any logged action.
Derived membership tables (which census ids a label or person covers) live
here too and are rebuilt by :mod:`.membership`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS label (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL DEFAULT 'tag' CHECK (kind IN ('tag', 'project')),
    doi TEXT,
    description TEXT,
    authors TEXT
);
CREATE TABLE IF NOT EXISTS label_link (
    label_id INTEGER NOT NULL REFERENCES label (id),
    target_type TEXT NOT NULL CHECK (target_type IN ('run', 'file', 'folder')),
    target TEXT NOT NULL,
    PRIMARY KEY (label_id, target_type, target)
);
CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    note TEXT,
    kind TEXT NOT NULL DEFAULT 'person',
    username TEXT,
    role TEXT,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS person_username ON person (username COLLATE NOCASE)
    WHERE username IS NOT NULL;
CREATE TABLE IF NOT EXISTS person_alias (
    kind TEXT NOT NULL CHECK (kind IN ('experimenter', 'home', 'other')),
    value TEXT NOT NULL,
    person_id INTEGER NOT NULL REFERENCES person (id),
    PRIMARY KEY (kind, value)
);
CREATE TABLE IF NOT EXISTS note (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL CHECK (target_type IN ('run', 'file', 'label', 'person')),
    target TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL,
    who TEXT
);
CREATE INDEX IF NOT EXISTS note_target ON note (target_type, target);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    who TEXT,
    action TEXT NOT NULL,
    summary TEXT,
    changes TEXT NOT NULL,
    undone_at TEXT,
    undone_by TEXT
);
CREATE TABLE IF NOT EXISTS run_label (
    target_id INTEGER NOT NULL, label_id INTEGER NOT NULL, via TEXT,
    PRIMARY KEY (target_id, label_id)
);
CREATE TABLE IF NOT EXISTS file_label (
    target_id INTEGER NOT NULL, label_id INTEGER NOT NULL, via TEXT,
    PRIMARY KEY (target_id, label_id)
);
CREATE TABLE IF NOT EXISTS run_person (
    target_id INTEGER NOT NULL, person_id INTEGER NOT NULL, via TEXT,
    PRIMARY KEY (target_id, person_id)
);
CREATE TABLE IF NOT EXISTS file_person (
    target_id INTEGER NOT NULL, person_id INTEGER NOT NULL,
    PRIMARY KEY (target_id, person_id)
);
CREATE INDEX IF NOT EXISTS run_label_label ON run_label (label_id);
CREATE INDEX IF NOT EXISTS file_label_label ON file_label (label_id);
CREATE INDEX IF NOT EXISTS run_person_person ON run_person (person_id);
CREATE INDEX IF NOT EXISTS file_person_person ON file_person (person_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Primary keys of the curated tables (what :class:`Change` and :func:`undo` address rows by).
KEYS = {
    "label": ("id",),
    "label_link": ("label_id", "target_type", "target"),
    "person": ("id",),
    "person_alias": ("kind", "value"),
    "note": ("id",),
}


class CurationError(ValueError):
    """A curation request that cannot be carried out (shown to the user)."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open (creating if needed) the curation file for writing.

    The connection is in autocommit mode: :class:`Change` and :func:`undo`
    open their own transactions.
    """
    conn = sqlite3.connect(os.fspath(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _upgrade(conn)
    conn.executescript(SCHEMA)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _upgrade(conn: sqlite3.Connection) -> None:
    """Bring curation files written by earlier versions up to the current schema."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "person" in tables:
        columns = _columns(conn, "person")
        for column, definition in (
            ("kind", "TEXT NOT NULL DEFAULT 'person'"),
            ("username", "TEXT"),
            ("role", "TEXT"),
            ("active", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE person ADD COLUMN {column} {definition}")
    if "run_person" in tables and "via" not in _columns(conn, "run_person"):
        conn.execute("DROP TABLE run_person")  # derived: rebuilt by membership.rebuild
        conn.execute("DELETE FROM meta WHERE key = 'built_for'")


def _where(key: dict[str, Any]) -> tuple[str, list]:
    return " AND ".join(f"{k} = ?" for k in key), list(key.values())


class Change:
    """Rows touched by one user action, recorded so the action can be undone.

    Use as a context manager: on success the action and its row changes are
    written to ``audit`` in the same transaction.
    """

    def __init__(self, conn: sqlite3.Connection, who: str | None, action: str, summary: str):
        self.conn, self.who, self.action, self.summary = conn, who, action, summary
        self.ops: list[dict[str, Any]] = []
        self.audit_id: int | None = None

    def __enter__(self) -> "Change":
        self.conn.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.conn.rollback()
            return
        if self.ops:
            cur = self.conn.execute(
                "INSERT INTO audit (at, who, action, summary, changes) VALUES (?, ?, ?, ?, ?)",
                (now(), self.who, self.action, self.summary, json.dumps(self.ops)),
            )
            self.audit_id = cur.lastrowid
        self.conn.commit()

    def _row(self, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
        clause, params = _where(key)
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {clause}", params).fetchone()
        return dict(row) if row else None

    def insert(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a row (ignored if its key already exists) and return it."""
        columns = list(values)
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})",
            list(values.values()),
        )
        if cur.rowcount == 0:
            return self._row(table, {k: values[k] for k in KEYS[table] if k in values}) or values
        row = dict(
            self.conn.execute(f"SELECT * FROM {table} WHERE rowid = ?", (cur.lastrowid,)).fetchone()
        )
        self.ops.append({"table": table, "before": None, "after": row})
        return row

    def update(self, table: str, key: dict[str, Any], values: dict[str, Any]) -> None:
        before = self._row(table, key)
        if before is None:
            raise CurationError(f"no such {table}")
        clause, params = _where(key)
        assignments = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {clause}", [*values.values(), *params]
        )
        after = self._row(table, {k: (values.get(k, key[k])) for k in key})
        if after != before:
            self.ops.append({"table": table, "before": before, "after": after})

    def delete(self, table: str, key: dict[str, Any]) -> None:
        before = self._row(table, key)
        if before is None:
            return
        clause, params = _where(key)
        self.conn.execute(f"DELETE FROM {table} WHERE {clause}", params)
        self.ops.append({"table": table, "before": before, "after": None})


def undo(conn: sqlite3.Connection, audit_id: int, who: str | None) -> dict[str, Any]:
    """Reverse one logged action, row by row, newest change first.

    Raises:
        CurationError: If the action is unknown, already undone, or later
            edits make it impossible to reverse cleanly.
    """
    entry = conn.execute("SELECT * FROM audit WHERE id = ?", (audit_id,)).fetchone()
    if entry is None:
        raise CurationError("no such change")
    if entry["undone_at"]:
        raise CurationError("this change was already undone")
    ops = json.loads(entry["changes"])
    try:
        conn.execute("BEGIN")
        for op in reversed(ops):
            table, before, after = op["table"], op["before"], op["after"]
            key_of = lambda row: {k: row[k] for k in KEYS[table]}  # noqa: E731
            if before is None:
                clause, params = _where(key_of(after))
                conn.execute(f"DELETE FROM {table} WHERE {clause}", params)
            elif after is None:
                columns = list(before)
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                    list(before.values()),
                )
            else:
                clause, params = _where(key_of(after))
                assignments = ", ".join(f"{k} = ?" for k in before)
                conn.execute(
                    f"UPDATE {table} SET {assignments} WHERE {clause}", [*before.values(), *params]
                )
        conn.execute(
            "UPDATE audit SET undone_at = ?, undone_by = ? WHERE id = ?", (now(), who, audit_id)
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise CurationError(f"cannot undo: later changes conflict ({exc})") from exc
    return dict(entry)
