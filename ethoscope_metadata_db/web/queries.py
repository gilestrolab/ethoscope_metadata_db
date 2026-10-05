"""SQL behind each page, over the census and its summary tables (read-only)."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import pandas as pd

from ..census import report
from ..census.normalise import normalise_machine_name
from .filters import Facet, LinkFacet, Selection, Table, where

CLAIMED = ("single", "multiple")
CATEGORY_ORDER = (
    "single",
    "multiple",
    "tentative",
    "duplicate",
    "superseded",
    "misfire",
    "empty",
    "orphan",
)
METADATA_OUTCOMES = ("all matched", "some unmatched", "none matched", "empty")

RUN_LABEL = LinkFacet(
    "label", "id", "Label", limit=30, link="cur.run_label", entity="cur.label", entity_id="label_id"
)
RUN_PERSON = LinkFacet(
    "person",
    "id",
    "Person",
    limit=30,
    link="cur.run_person",
    entity="cur.person",
    entity_id="person_id",
)
RUNS = Table(
    name="db_summary",
    facets=(
        Facet("category", "category", "Category"),
        RUN_LABEL,
        RUN_PERSON,
        Facet("year", "year", "Year", limit=30, by_value=True),
        Facet("machine", "machine_name", "Machine", limit=30),
        Facet("month", "month", "Month", limit=0),
        Facet("day", "substr(start_local, 1, 10)", "Day", limit=0),
        Facet("experimenter", "experimenter", "Experimenter (as typed)", limit=30, primary=False),
        Facet("stack", "stack", "Camera stack", primary=False),
        Facet("location", "location", "Location", limit=30, primary=False),
        Facet("interactor", "interactor", "Interactor", limit=30, primary=False),
        Facet("rois", "n_rois", "ROIs", limit=8, primary=False),
    ),
    text_columns=("path", "machine_name", "experimenter", "location", "homes"),
    sorts={
        "start": "start_local",
        "machine": "machine_name",
        "experimenter": "experimenter",
        "category": "category",
        "rows": "n_rows",
        "sources": "n_sources",
        "size": "size",
        "length": "duration_h",
    },
    default_sort="start",
)

FILES = Table(
    name="file_summary",
    facets=(
        Facet("outcome", "outcome", "Outcome"),
        LinkFacet(
            "label",
            "id",
            "Label",
            limit=30,
            link="cur.file_label",
            entity="cur.label",
            entity_id="label_id",
        ),
        LinkFacet(
            "person",
            "id",
            "Person",
            limit=30,
            link="cur.file_person",
            entity="cur.person",
            entity_id="person_id",
        ),
        Facet("tag", "tag", "Location"),
        Facet("year", "year", "First year", limit=30, by_value=True),
        Facet("home", "home", "Home folder", limit=30, primary=False),
        Facet("order", "date_order", "Date order", limit=8, primary=False),
        Facet("status", "status", "Status", limit=8, primary=False),
    ),
    text_columns=("path", "sheet", "home"),
    sorts={
        "modified": "mtime",
        "path": "path",
        "rows": "n_rows",
        "dbs": "n_dbs",
        "matched": "n_unique",
        "not_found": "n_not_found",
        "first": "date_min",
    },
    default_sort="modified",
)


def _placeholders(values: tuple | list) -> str:
    return ", ".join("?" * len(values))


def summarised_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM schema_info WHERE key = 'summarised_at'").fetchone()
    return row[0] if row else None


# ---- overview ---------------------------------------------------------------------------


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """Headline counts and the breakdowns the overview page charts."""
    categories = dict(
        conn.execute("SELECT category, count(*) FROM db_summary GROUP BY 1").fetchall()
    )
    monthly: dict[str, dict[str, int]] = {}
    for month, category, n in conn.execute(
        "SELECT month, category, count(*) FROM db_summary WHERE month IS NOT NULL "
        "GROUP BY 1, 2 ORDER BY 1"
    ):
        monthly.setdefault(month, {})[category] = n
    yearly = _crosstab(conn, "year")
    stacks = _crosstab(conn, "coalesce(stack, 'no kernel')")
    people = conn.execute(
        "SELECT coalesce(nullif(experimenter, ''), '(none)') AS who, "
        "sum(category = 'orphan') AS orphan, sum(category = 'misfire') AS misfire, "
        f"sum(category IN ({_placeholders(CLAIMED)})) AS claimed, count(*) AS total "
        "FROM db_summary GROUP BY who ORDER BY orphan DESC, total DESC LIMIT 15",
        CLAIMED,
    ).fetchall()
    sums = ", ".join(
        f'sum(n_{s}) AS "{s}"'
        for s in (
            "unique",
            "not_found",
            "roi_missing",
            "roi_unknown",
            "date_ambiguous",
            "ambiguous",
            "incomplete",
        )
    )
    rows = conn.execute(
        f"SELECT count(*) AS files, {sums} FROM file_summary "
        f"WHERE outcome IN ({_placeholders(METADATA_OUTCOMES)})",
        METADATA_OUTCOMES,
    ).fetchone()
    tags = conn.execute(
        "SELECT coalesce(tag, '(none)') AS tag, count(*) AS files, sum(n_rows) AS rows, "
        "sum(n_dbs) AS dbs FROM file_summary "
        f"WHERE outcome IN ({_placeholders(METADATA_OUTCOMES)}) GROUP BY 1 ORDER BY files DESC",
        METADATA_OUTCOMES,
    ).fetchall()
    return {
        "total": sum(categories.values()),
        "categories": categories,
        "claimed": sum(categories.get(c, 0) for c in CLAIMED),
        "monthly": monthly,
        "yearly": yearly,
        "stacks": stacks,
        "people": people,
        "rows": dict(rows) if rows else {},
        "tags": tags,
        "summarised_at": summarised_at(conn),
    }


def _crosstab(conn: sqlite3.Connection, key: str) -> list[dict[str, Any]]:
    table: dict[str, dict[str, int]] = {}
    for value, category, n in conn.execute(
        f"SELECT {key}, category, count(*) FROM db_summary GROUP BY 1, 2 ORDER BY 1"
    ):
        table.setdefault(value or "(none)", {})[category] = n
    return [{"key": k, "counts": v, "total": sum(v.values())} for k, v in table.items()]


# ---- runs ---------------------------------------------------------------------------------


def run_detail(conn: sqlite3.Connection, db_id: int) -> dict[str, Any] | None:
    """Everything the run page shows: facts, tubes and their annotations, siblings."""
    run = conn.execute("SELECT * FROM db_summary WHERE id = ?", (db_id,)).fetchone()
    if run is None:
        return None
    raw = conn.execute("SELECT * FROM db_file WHERE id = ?", (db_id,)).fetchone()
    claims = conn.execute(
        "SELECT m.status, m.method, m.note, r.id AS row_id, r.row_idx, r.region_id, r.date, "
        "r.start_time, r.sex, r.genotype_raw AS genotype, r.treatment_raw AS treatment, "
        "r.other_json, f.id AS file_id, f.path, f.sheet, f.home "
        "FROM match m JOIN meta_row r ON r.id = m.meta_row_id "
        "JOIN meta_file f ON f.id = r.meta_file_id WHERE m.db_file_id = ? "
        "ORDER BY r.region_id, f.path, r.row_idx",
        (db_id,),
    ).fetchall()
    rois = json.loads(raw["roi_ids"]) if raw and raw["roi_ids"] else []
    tubes: dict[int, dict[str, Any]] = {
        r: {"region": r, "groups": {}, "in_map": True} for r in rois
    }
    passed_over = {"superseded": set(), "duplicate": set()}
    for claim in claims:
        if claim["status"] in report.LINK_STATUSES:
            passed_over[claim["status"]].add(claim["row_id"])
            continue
        tube = tubes.setdefault(
            claim["region_id"], {"region": claim["region_id"], "groups": {}, "in_map": False}
        )
        # Reason: copies of one metadata file repeat the same annotation many times;
        # group identical annotations and count the files behind each.
        key = (claim["status"], claim["genotype"], claim["sex"], claim["treatment"])
        group = tube["groups"].setdefault(
            key,
            {
                "status": key[0],
                "genotype": key[1],
                "sex": key[2],
                "treatment": key[3],
                "files": {},
                "notes": set(),
                "other_json": claim["other_json"],
            },
        )
        group["files"].setdefault(claim["file_id"], (claim["path"], claim["row_idx"]))
        if claim["note"]:
            group["notes"].add(claim["note"])
    shown = {"genotype": False, "sex": False, "treatment": False, "other": False}
    for tube in tubes.values():
        tube["groups"] = sorted(tube["groups"].values(), key=lambda g: -len(g["files"]))
        for g in tube["groups"]:
            g["other"] = sorted(json.loads(g["other_json"]).items()) if g["other_json"] else []
            for key in ("genotype", "sex", "treatment"):
                shown[key] |= bool(g[key])
            shown["other"] |= bool(g["other"])
        keys = {
            re.sub(r"[^a-z0-9]", "", (g["genotype"] or "").lower())
            for g in tube["groups"]
            if g["status"] in report.CLAIM_STATUSES and g["genotype"]
        }
        tube["conflict"] = len(keys) > 1
    day = (run["start_local"] or "")[:10]
    same_day = conn.execute(
        "SELECT id, start_local, category, size, n_rows, path FROM db_summary "
        "WHERE machine_name = ? AND substr(start_local, 1, 10) = ? ORDER BY start_local",
        (run["machine_name"], day),
    ).fetchall()
    return {
        "run": run,
        "raw": raw,
        "tubes": [tubes[k] for k in sorted(tubes)],
        "files": sorted(
            {
                (c["file_id"], c["path"], c["sheet"])
                for c in claims
                if c["status"] not in report.LINK_STATUSES
            }
        ),
        "passed_over": {k: len(v) for k, v in passed_over.items()},
        "same_day": same_day,
        "shown": shown,
    }


# ---- files --------------------------------------------------------------------------------


def file_detail(conn: sqlite3.Connection, file_id: int, limit: int = 500) -> dict[str, Any] | None:
    """Everything the metadata file page shows."""
    summary = conn.execute("SELECT * FROM file_summary WHERE id = ?", (file_id,)).fetchone()
    if summary is None:
        return None
    raw = conn.execute("SELECT * FROM meta_file WHERE id = ?", (file_id,)).fetchone()
    copies = conn.execute(
        "SELECT id, path, sheet, home, tag FROM file_summary WHERE sha256 = ? AND id != ? "
        f"AND outcome IN ({_placeholders(METADATA_OUTCOMES)}) ORDER BY path",
        (summary["sha256"], file_id, *METADATA_OUTCOMES),
    ).fetchall()
    unmatched = conn.execute(
        "SELECT r.row_idx, r.date, r.date_alt, r.start_time, r.machine_name, r.region_id, "
        "r.genotype_raw AS genotype, m.status, group_concat(DISTINCT m.note) AS note "
        "FROM meta_row r JOIN match m ON m.meta_row_id = r.id WHERE r.meta_file_id = ? "
        "AND m.status NOT IN ('unique', 'superseded', 'duplicate') "
        "GROUP BY r.id, m.status ORDER BY r.row_idx LIMIT ?",
        (file_id, limit),
    ).fetchall()
    runs = conn.execute(
        "SELECT d.id, d.start_local, d.machine_name, d.category, d.experimenter, "
        "count(DISTINCT r.id) AS n_rows FROM match m JOIN meta_row r ON r.id = m.meta_row_id "
        "JOIN db_summary d ON d.id = m.db_file_id WHERE r.meta_file_id = ? "
        f"AND m.status IN ({_placeholders(report.CLAIM_STATUSES)}) "
        "GROUP BY d.id ORDER BY d.start_local",
        (file_id, *report.CLAIM_STATUSES),
    ).fetchall()
    return {
        "file": summary,
        "raw": raw,
        "columns": json.loads(raw["columns_raw"]) if raw["columns_raw"] else [],
        "column_map": json.loads(raw["column_map"]) if raw["column_map"] else {},
        "copies": copies,
        "unmatched": unmatched,
        "runs": runs,
    }


_TUBES_SQL = (
    "SELECT r.machine_name, r.date, d.path_start, r.region_id, r.genotype_raw AS genotype, "
    "r.sex, r.treatment_raw AS treatment, d.path FROM match m "
    "JOIN meta_row r ON r.id = m.meta_row_id JOIN db_file d ON d.id = m.db_file_id "
    "WHERE m.status = 'unique' AND {condition} ORDER BY d.path, r.region_id"
)


def _ethoscopy(frame: pd.DataFrame) -> pd.DataFrame:
    frame["time"] = frame["path_start"].str[11:19].str.replace(":", "-")
    return report.to_ethoscopy(frame).assign(path=frame["path"].to_numpy())


def file_tubes(conn: sqlite3.Connection, file_id: int) -> pd.DataFrame:
    """The file's uniquely matched tubes, as an ethoscopy metadata table (+ ``path``)."""
    sql = _TUBES_SQL.format(condition="r.meta_file_id = ?")
    return _ethoscopy(pd.read_sql_query(sql, conn, params=(file_id,)))


def selection_tubes(conn: sqlite3.Connection, selection: Selection) -> pd.DataFrame:
    """Uniquely matched tubes of every run in a Runs selection, for ethoscopy."""
    clause, params = where(RUNS, selection)
    sql = _TUBES_SQL.format(condition=f"m.db_file_id IN (SELECT id FROM db_summary {clause})")
    return _ethoscopy(pd.read_sql_query(sql, conn, params=params))


def selection_runs(conn: sqlite3.Connection, selection: Selection) -> pd.DataFrame:
    """The runs of a selection, one row each, for a CSV download."""
    clause, params = where(RUNS, selection)
    columns = (
        "path, machine_name, start_local, experimenter, location, stack, interactor, n_rois, "
        "size, duration_h, category, n_rows, n_sources, homes"
    )
    return pd.read_sql_query(
        f"SELECT {columns} FROM db_summary {clause} ORDER BY start_local", conn, params=params
    )


# ---- search -------------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def search(conn: sqlite3.Connection, q: str, limit: int = 15) -> dict[str, Any]:
    """Results for the search box, grouped by kind."""
    q = q.strip()
    if not q:
        return {}
    like = f"%{q}%"
    out: dict[str, Any] = {}
    machine = normalise_machine_name(q) if re.search(r"\d", q) else None
    if machine and machine.startswith("ETHOSCOPE_"):
        n = conn.execute(
            "SELECT count(*) FROM db_summary WHERE machine_name = ?", (machine,)
        ).fetchone()[0]
        if n:
            out["machine"] = {"name": machine, "n": n}
    if _DATE_RE.match(q):
        n = conn.execute(
            "SELECT count(*) FROM db_summary WHERE start_local LIKE ?", (q + "%",)
        ).fetchone()[0]
        out["date"] = {"value": q, "n": n}
    out["experimenters"] = conn.execute(
        "SELECT experimenter AS value, count(*) AS n FROM db_summary WHERE experimenter LIKE ? "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
        (like, limit),
    ).fetchall()
    out["homes"] = conn.execute(
        "SELECT home AS value, count(*) AS n FROM file_summary WHERE home LIKE ? "
        f"AND outcome IN ({_placeholders(METADATA_OUTCOMES)}) GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
        (like, *METADATA_OUTCOMES, limit),
    ).fetchall()
    out["files"] = conn.execute(
        "SELECT id, path, sheet, outcome, n_rows FROM file_summary WHERE path LIKE ? "
        f"AND outcome IN ({_placeholders(METADATA_OUTCOMES)}) ORDER BY mtime DESC LIMIT ?",
        (like, *METADATA_OUTCOMES, limit),
    ).fetchall()
    out["runs"] = conn.execute(
        "SELECT id, path, start_local, machine_name, category FROM db_summary WHERE path LIKE ? "
        "ORDER BY start_local DESC LIMIT ?",
        (like, limit),
    ).fetchall()
    out["genotypes"] = conn.execute(
        "SELECT genotype AS value, n_rows AS n, n_files FROM genotype_summary "
        "WHERE genotype LIKE ? ORDER BY n_rows DESC LIMIT ?",
        (like, limit),
    ).fetchall()
    return out
