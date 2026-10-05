"""Which census runs and files each label and person covers (derived, rebuilt on demand).

Labels and aliases are keyed by paths and strings; the census by ids that
change when it is rebuilt. These tables translate one into the other:

``file_label``  files a label covers: linked directly, or inside a labelled folder
``run_label``   runs a label covers: linked directly, inside a labelled folder,
                or claimed by a labelled metadata file (``via`` says which)
``file_person`` files in a home that is one of a person's aliases
``run_person``  runs whose experimenter string is one of a person's aliases

They are rebuilt after every curation change and whenever the census was
summarised again (``meta.built_for`` records which summary they match).
"""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict

from ...census import report

_lock = threading.Lock()


def _like_prefix(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.rstrip("/") + "/%"


def census_stamp(census: sqlite3.Connection) -> str | None:
    row = census.execute("SELECT value FROM schema_info WHERE key = 'summarised_at'").fetchone()
    return row[0] if row else None


def rebuild(census: sqlite3.Connection, cur: sqlite3.Connection) -> dict[str, int]:
    """Recompute the membership tables from the curated links and aliases."""
    links = cur.execute("SELECT label_id, target_type, target FROM label_link").fetchall()
    file_rows: dict[tuple[int, int], str] = {}
    run_rows: dict[tuple[int, int], str] = {}
    for link in links:
        label_id, kind, target = link["label_id"], link["target_type"], link["target"]
        if kind == "file":
            for (fid,) in census.execute("SELECT id FROM file_summary WHERE path = ?", (target,)):
                file_rows[(fid, label_id)] = "file"
        elif kind == "run":
            for (rid,) in census.execute("SELECT id FROM db_summary WHERE path = ?", (target,)):
                run_rows[(rid, label_id)] = "run"
        else:
            pattern = _like_prefix(target)
            for (fid,) in census.execute(
                "SELECT id FROM file_summary WHERE path LIKE ? ESCAPE '\\'", (pattern,)
            ):
                file_rows.setdefault((fid, label_id), "folder")
            for (rid,) in census.execute(
                "SELECT id FROM db_summary WHERE path LIKE ? ESCAPE '\\'", (pattern,)
            ):
                run_rows.setdefault((rid, label_id), "folder")
    labels_of_file: dict[int, set[int]] = defaultdict(set)
    for fid, label_id in file_rows:
        labels_of_file[fid].add(label_id)
    if labels_of_file:
        file_ids = list(labels_of_file)
        statuses = report.CLAIM_STATUSES
        for start in range(0, len(file_ids), 500):
            chunk = file_ids[start : start + 500]
            rows = census.execute(
                "SELECT DISTINCT r.meta_file_id, m.db_file_id FROM match m "
                "JOIN meta_row r ON r.id = m.meta_row_id "
                f"WHERE r.meta_file_id IN ({', '.join('?' * len(chunk))}) "
                f"AND m.status IN ({', '.join('?' * len(statuses))})",
                [*chunk, *statuses],
            )
            for fid, rid in rows:
                for label_id in labels_of_file[fid]:
                    run_rows.setdefault((rid, label_id), "metadata")

    aliases = cur.execute("SELECT kind, value, person_id FROM person_alias").fetchall()
    by_experimenter = defaultdict(set)
    by_home = defaultdict(set)
    for alias in aliases:
        if alias["kind"] == "experimenter":
            by_experimenter[alias["value"]].add(alias["person_id"])
        elif alias["kind"] == "home":
            by_home[alias["value"]].add(alias["person_id"])
    run_people = [
        (rid, pid)
        for rid, experimenter in census.execute(
            "SELECT id, experimenter FROM db_summary WHERE experimenter IS NOT NULL"
        )
        for pid in by_experimenter.get(experimenter, ())
    ]
    file_people = [
        (fid, pid)
        for fid, home in census.execute("SELECT id, home FROM file_summary WHERE home IS NOT NULL")
        for pid in by_home.get(home, ())
    ]

    cur.execute("BEGIN")
    for table in ("file_label", "run_label", "run_person", "file_person"):
        cur.execute(f"DELETE FROM {table}")
    cur.executemany(
        "INSERT INTO file_label VALUES (?, ?, ?)", [(f, l, v) for (f, l), v in file_rows.items()]
    )
    cur.executemany(
        "INSERT INTO run_label VALUES (?, ?, ?)", [(r, l, v) for (r, l), v in run_rows.items()]
    )
    cur.executemany("INSERT INTO run_person VALUES (?, ?)", run_people)
    cur.executemany("INSERT INTO file_person VALUES (?, ?)", file_people)
    cur.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('built_for', ?)",
        (census_stamp(census) or "",),
    )
    cur.execute("COMMIT")
    return {
        "file_label": len(file_rows),
        "run_label": len(run_rows),
        "run_person": len(run_people),
        "file_person": len(file_people),
    }


def ensure_fresh(census: sqlite3.Connection, cur: sqlite3.Connection, force: bool = False) -> bool:
    """Rebuild if the census was summarised since the last build (or ``force``).

    Returns:
        Whether a rebuild happened.
    """
    with _lock:
        built = cur.execute("SELECT value FROM meta WHERE key = 'built_for'").fetchone()
        if not force and built is not None and built[0] == (census_stamp(census) or ""):
            return False
        rebuild(census, cur)
        return True
