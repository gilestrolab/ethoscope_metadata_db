"""Read-only queries over the curation file, attached to the census connection as ``cur``."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, Iterable


def labels_by_id(
    conn: sqlite3.Connection, link: str, ids: Iterable[int]
) -> dict[int, list[sqlite3.Row]]:
    """Labels of each listed run or file (``link``: ``run_label`` or ``file_label``)."""
    ids = list(ids)
    out: dict[int, list[sqlite3.Row]] = defaultdict(list)
    if not ids:
        return out
    rows = conn.execute(
        f"SELECT m.target_id, l.id, l.name, l.kind, m.via FROM cur.{link} m "
        f"JOIN cur.label l ON l.id = m.label_id WHERE m.target_id IN ({', '.join('?' * len(ids))}) "
        "ORDER BY l.kind DESC, l.name",
        ids,
    )
    for row in rows:
        out[row["target_id"]].append(row)
    return out


def people_by_id(
    conn: sqlite3.Connection, link: str, ids: Iterable[int]
) -> dict[int, list[sqlite3.Row]]:
    """People of each listed run or file (``link``: ``run_person`` or ``file_person``)."""
    ids = list(ids)
    out: dict[int, list[sqlite3.Row]] = defaultdict(list)
    if not ids:
        return out
    rows = conn.execute(
        f"SELECT m.target_id, p.id, p.name FROM cur.{link} m JOIN cur.person p ON p.id = m.person_id "
        f"WHERE m.target_id IN ({', '.join('?' * len(ids))}) ORDER BY p.name",
        ids,
    )
    for row in rows:
        out[row["target_id"]].append(row)
    return out


def notes(conn: sqlite3.Connection, target_type: str, target: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cur.note WHERE target_type = ? AND target = ? ORDER BY at",
        (target_type, target),
    ).fetchall()


def all_labels(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every label with how many links, files and runs it has."""
    return conn.execute(
        "SELECT l.*, "
        "(SELECT count(*) FROM cur.label_link k WHERE k.label_id = l.id) AS n_links, "
        "(SELECT count(*) FROM cur.file_label f WHERE f.label_id = l.id) AS n_files, "
        "(SELECT count(*) FROM cur.run_label r WHERE r.label_id = l.id) AS n_runs "
        "FROM cur.label l ORDER BY l.kind DESC, l.name"
    ).fetchall()


def label_detail(conn: sqlite3.Connection, label_id: int) -> dict[str, Any] | None:
    label = conn.execute("SELECT * FROM cur.label WHERE id = ?", (label_id,)).fetchone()
    if label is None:
        return None
    links = conn.execute(
        "SELECT target_type, target FROM cur.label_link WHERE label_id = ? ORDER BY target_type, target",
        (label_id,),
    ).fetchall()
    categories = dict(
        conn.execute(
            "SELECT d.category, count(*) FROM cur.run_label m JOIN db_summary d ON d.id = m.target_id "
            "WHERE m.label_id = ? GROUP BY 1",
            (label_id,),
        ).fetchall()
    )
    via = dict(
        conn.execute(
            "SELECT via, count(*) FROM cur.run_label WHERE label_id = ? GROUP BY 1", (label_id,)
        ).fetchall()
    )
    monthly: dict[str, dict[str, int]] = {}
    for month, category, n in conn.execute(
        "SELECT d.month, d.category, count(*) FROM cur.run_label m JOIN db_summary d ON d.id = m.target_id "
        "WHERE m.label_id = ? GROUP BY 1, 2",
        (label_id,),
    ):
        monthly.setdefault(month, {})[category] = n
    n_files = conn.execute(
        "SELECT count(*) FROM cur.file_label WHERE label_id = ?", (label_id,)
    ).fetchone()[0]
    return {
        "label": label,
        "links": links,
        "categories": categories,
        "via": via,
        "monthly": monthly,
        "n_runs": sum(categories.values()),
        "n_files": n_files,
        "notes": notes(conn, "label", str(label_id)),
    }


def all_people(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every person with their aliases and run counts."""
    people = []
    for person in conn.execute("SELECT * FROM cur.person ORDER BY name").fetchall():
        aliases = conn.execute(
            "SELECT kind, value FROM cur.person_alias WHERE person_id = ? ORDER BY kind, value",
            (person["id"],),
        ).fetchall()
        counts = dict(
            conn.execute(
                "SELECT d.category, count(*) FROM cur.run_person m JOIN db_summary d ON d.id = m.target_id "
                "WHERE m.person_id = ? GROUP BY 1",
                (person["id"],),
            ).fetchall()
        )
        n_files = conn.execute(
            "SELECT count(*) FROM cur.file_person WHERE person_id = ?", (person["id"],)
        ).fetchone()[0]
        people.append(
            {
                "person": person,
                "aliases": aliases,
                "counts": counts,
                "n_runs": sum(counts.values()),
                "n_files": n_files,
            }
        )
    return people


def person_detail(conn: sqlite3.Connection, person_id: int) -> dict[str, Any] | None:
    person = conn.execute("SELECT * FROM cur.person WHERE id = ?", (person_id,)).fetchone()
    if person is None:
        return None
    aliases = conn.execute(
        "SELECT kind, value FROM cur.person_alias WHERE person_id = ? ORDER BY kind, value",
        (person_id,),
    ).fetchall()
    monthly: dict[str, dict[str, int]] = {}
    categories: dict[str, int] = defaultdict(int)
    for month, category, n in conn.execute(
        "SELECT d.month, d.category, count(*) FROM cur.run_person m JOIN db_summary d ON d.id = m.target_id "
        "WHERE m.person_id = ? GROUP BY 1, 2",
        (person_id,),
    ):
        monthly.setdefault(month, {})[category] = n
        categories[category] += n
    machines = conn.execute(
        "SELECT d.machine_name, count(*) AS n FROM cur.run_person m JOIN db_summary d ON d.id = m.target_id "
        "WHERE m.person_id = ? GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
        (person_id,),
    ).fetchall()
    files = conn.execute(
        "SELECT f.outcome, count(*) AS n FROM cur.file_person m JOIN file_summary f ON f.id = m.target_id "
        "WHERE m.person_id = ? GROUP BY 1 ORDER BY 2 DESC",
        (person_id,),
    ).fetchall()
    return {
        "person": person,
        "aliases": aliases,
        "monthly": monthly,
        "categories": dict(categories),
        "n_runs": sum(categories.values()),
        "machines": machines,
        "files": files,
        "notes": notes(conn, "person", str(person_id)),
    }


def recent_changes(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, at, who, action, summary, undone_at, undone_by FROM cur.audit ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
