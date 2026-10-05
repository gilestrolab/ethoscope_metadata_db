"""Which census runs and files each label and person covers (derived, rebuilt on demand).

Labels and aliases are keyed by paths and strings; the census by ids that
change when it is rebuilt. These tables translate one into the other:

``file_label``  files a label covers: linked directly, or inside a labelled folder
``run_label``   runs a label covers: linked directly, inside a labelled folder,
                or claimed by a labelled metadata file (``via`` says which)
``file_person`` each metadata sheet's owner: the real person whose alias is its
                home (spreadsheets that are not metadata have no owner)
``run_person``  each run's owner, at most one: the real person whose alias is
                its experimenter string (``via='experimenter'``), otherwise the
                only real person among the owners of the metadata files that
                claim it (``via='metadata'``); none when there are several

Only people of kind ``person`` own anything; names marked ``shared`` (guest
accounts, project folders, teams) are reviewed but own nothing. The tables
are rebuilt after every curation change and whenever the census was
summarised again or these rules changed (``meta.built_for`` records which
summary and which :data:`RULES_VERSION` they match).
"""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict

from .suggest import METADATA_OUTCOMES

_lock = threading.Lock()
RULES_VERSION = "2"  # bump when the rules below change, so existing files get rebuilt


def _like_prefix(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.rstrip("/") + "/%"


def census_stamp(census: sqlite3.Connection) -> str | None:
    row = census.execute("SELECT value FROM schema_info WHERE key = 'summarised_at'").fetchone()
    return row[0] if row else None


def build_stamp(census: sqlite3.Connection) -> str:
    """What the membership tables were built for: the census summary and these rules."""
    return f"{census_stamp(census) or ''} rules {RULES_VERSION}"


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
    claims = census.execute("SELECT db_id, file_id FROM claim_summary").fetchall()
    for rid, fid in claims:
        for label_id in labels_of_file.get(fid, ()):
            run_rows.setdefault((rid, label_id), "metadata")

    people = cur.execute(
        "SELECT a.kind, a.value, a.person_id FROM person_alias a "
        "JOIN person p ON p.id = a.person_id WHERE p.kind = 'person'"
    ).fetchall()
    by_experimenter = {a["value"]: a["person_id"] for a in people if a["kind"] == "experimenter"}
    by_home = {a["value"]: a["person_id"] for a in people if a["kind"] == "home"}
    file_owner = {
        fid: by_home[home]
        for fid, home in census.execute(
            "SELECT id, home FROM file_summary WHERE home IS NOT NULL "
            f"AND outcome IN ({', '.join('?' * len(METADATA_OUTCOMES))})",
            METADATA_OUTCOMES,
        )
        if home in by_home
    }
    run_owner: dict[int, tuple[int, str]] = {}
    for rid, experimenter in census.execute("SELECT id, experimenter FROM db_summary"):
        if experimenter in by_experimenter:
            run_owner[rid] = (by_experimenter[experimenter], "experimenter")
    owners_by_run: dict[int, set[int]] = defaultdict(set)
    for rid, fid in claims:
        if rid not in run_owner and fid in file_owner:
            owners_by_run[rid].add(file_owner[fid])
    for rid, owners in owners_by_run.items():
        # Reason: unambiguous only. Files in homes with no real owner (shared
        # folders, copies) are neutral; two real owners leave the run unowned.
        if len(owners) == 1:
            run_owner[rid] = (next(iter(owners)), "metadata")
    run_people = [(rid, pid, via) for rid, (pid, via) in run_owner.items()]
    file_people = list(file_owner.items())

    cur.execute("BEGIN")
    for table in ("file_label", "run_label", "run_person", "file_person"):
        cur.execute(f"DELETE FROM {table}")
    cur.executemany(
        "INSERT INTO file_label VALUES (?, ?, ?)", [(f, l, v) for (f, l), v in file_rows.items()]
    )
    cur.executemany(
        "INSERT INTO run_label VALUES (?, ?, ?)", [(r, l, v) for (r, l), v in run_rows.items()]
    )
    cur.executemany("INSERT INTO run_person VALUES (?, ?, ?)", run_people)
    cur.executemany("INSERT INTO file_person VALUES (?, ?)", file_people)
    cur.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('built_for', ?)",
        (build_stamp(census),),
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
        if not force and built is not None and built[0] == build_stamp(census):
            return False
        rebuild(census, cur)
        return True
