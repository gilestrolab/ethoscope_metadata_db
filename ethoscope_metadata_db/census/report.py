"""Census views: who claims which database, what is left over, and queries.

A database is *claimed* by a metadata row when matching identified it alone
(status ``unique``, ``roi_missing`` or ``roi_unknown``), *tentatively
claimed* when it is one of several candidates (``ambiguous``), *superseded*
when it is only an earlier same-day run (false start, early crash) passed
over for the last run of the day, and an *orphan* otherwise. Sources are
counted per metadata file path, so two sheets of one workbook are one source.
"""

from __future__ import annotations

import os
import sqlite3

import pandas as pd

CLAIM_STATUSES = ("unique", "roi_missing", "roi_unknown")
TENTATIVE_STATUSES = ("ambiguous",)
SUPERSEDED = "superseded"

_MATCHES_SQL = """
SELECT m.status, m.method, m.note, m.db_file_id,
       r.id AS meta_row_id, r.row_idx, r.date, r.start_time,
       r.machine_name, r.region_id, r.sex, r.genotype_raw AS genotype,
       r.treatment_raw AS treatment, r.other_json,
       f.path AS source, f.sheet, f.home,
       d.path AS db_path, d.path_start, d.start_utc, d.stack, d.tracker,
       d.interactor, d.kernel, d.camera_sensor, d.sw_version, d.duration_h
FROM match m
JOIN meta_row r ON r.id = m.meta_row_id
JOIN meta_file f ON f.id = r.meta_file_id
LEFT JOIN db_file d ON d.id = m.db_file_id
"""


def matches(conn: sqlite3.Connection) -> pd.DataFrame:
    """Return every ``match`` row joined with its metadata row, file and db."""
    return pd.read_sql_query(_MATCHES_SQL, conn)


def row_outcomes(conn: sqlite3.Connection) -> pd.DataFrame:
    """Matches without ``superseded`` links: these carry each row's status."""
    found = matches(conn)
    return found[found["status"] != SUPERSEDED]


def db_claims(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per indexed database with its claim category.

    Columns include ``n_rows`` (rows claiming it), ``n_sources``,
    ``sources`` (``|``-joined paths) and ``category``: ``single``,
    ``multiple``, ``tentative``, ``superseded`` or ``orphan``.
    """
    dbs = pd.read_sql_query("SELECT * FROM db_file ORDER BY path", conn)
    found = matches(conn).dropna(subset=["db_file_id"])
    found["db_file_id"] = found["db_file_id"].astype(int)
    claimed = found[found["status"].isin(CLAIM_STATUSES)]
    tentative = found[found["status"].isin(TENTATIVE_STATUSES)]
    superseded = found[found["status"] == SUPERSEDED]
    per_db = claimed.groupby("db_file_id").agg(
        n_rows=("meta_row_id", "nunique"),
        n_sources=("source", "nunique"),
        sources=("source", lambda s: "|".join(sorted(set(s)))),
    )
    dbs = dbs.merge(per_db, left_on="id", right_index=True, how="left")
    dbs["n_tentative"] = dbs["id"].map(tentative.groupby("db_file_id")["meta_row_id"].nunique())
    dbs["n_superseded"] = dbs["id"].map(superseded.groupby("db_file_id")["meta_row_id"].nunique())
    counts = ["n_rows", "n_sources", "n_tentative", "n_superseded"]
    dbs[counts] = dbs[counts].fillna(0).astype(int)
    dbs["category"] = "orphan"
    dbs.loc[dbs["n_superseded"] > 0, "category"] = SUPERSEDED
    dbs.loc[dbs["n_tentative"] > 0, "category"] = "tentative"
    dbs.loc[dbs["n_sources"] == 1, "category"] = "single"
    dbs.loc[dbs["n_sources"] > 1, "category"] = "multiple"
    dbs["year"] = dbs["path_start"].fillna(dbs["start_utc"]).str[:4]
    return dbs


def multiply_claimed(conn: sqlite3.Connection) -> pd.DataFrame:
    """Tubes (db, region) claimed by more than one source, and whether they agree.

    Genotypes are compared case-insensitively with surrounding spaces removed.
    """
    found = matches(conn)
    found = found[found["status"].isin(CLAIM_STATUSES)].copy()
    found["genotype_key"] = found["genotype"].fillna("").str.strip().str.lower()
    grouped = found.groupby(["db_path", "region_id"]).agg(
        n_sources=("source", "nunique"),
        sources=("source", lambda s: "|".join(sorted(set(s)))),
        genotypes=("genotype", lambda s: "|".join(sorted(set(s.dropna())))),
        n_genotypes=("genotype_key", "nunique"),
    )
    grouped = grouped[grouped["n_sources"] > 1].reset_index()
    grouped["genotype_agrees"] = grouped["n_genotypes"] <= 1
    return grouped.drop(columns="n_genotypes")


def unmatched_rows(conn: sqlite3.Connection) -> pd.DataFrame:
    """Metadata rows that did not resolve to one database with its region.

    One line per row, with every candidate database listed.
    """
    found = row_outcomes(conn)
    found = found[found["status"] != "unique"]
    if found.empty:
        return found
    keys = ["meta_row_id", "source", "sheet", "home", "row_idx", "status", "date"]
    keys += ["machine_name", "region_id", "genotype"]
    return (
        found.groupby(keys, dropna=False)
        .agg(
            candidates=("db_path", lambda s: "|".join(s.dropna())),
            note=("note", lambda s: "|".join(sorted(set(s.dropna())))),
        )
        .reset_index()
        .sort_values(["source", "sheet", "row_idx"])
    )


def summaries(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    """Counts of databases by year, machine, stack and tracker, and of
    metadata rows by home and status."""
    claims = db_claims(conn)
    out = {}
    for key in ("year", "machine_name", "stack", "tracker"):
        table = pd.crosstab(claims[key].fillna("unknown"), claims["category"])
        table["total"] = table.sum(axis=1)
        out[f"dbs_by_{key}"] = table
    first = row_outcomes(conn).drop_duplicates("meta_row_id")
    out["rows_by_home"] = pd.crosstab(first["home"].fillna(""), first["status"])
    return out


def annotated_tubes(conn: sqlite3.Connection) -> pd.DataFrame:
    """Uniquely matched tubes with their annotation and recording facts.

    Column names follow ethoscopy's metadata conventions (``machine_name``,
    ``date``, ``region_id``) so the output can be passed to ethoscopy or
    scopr; ``path`` is the database file. ``time`` is the run's stamp
    (``HH-MM-SS``), so ethoscopy loads exactly this run even when several
    started that day (without it ethoscopy keeps the largest file).
    """
    found = matches(conn)
    tubes = found[found["status"] == "unique"].drop(
        columns=["status", "method", "note", "db_file_id"]
    )
    tubes = tubes.rename(columns={"db_path": "path"})
    tubes["time"] = tubes["path_start"].str[11:19].str.replace(":", "-")
    return tubes.sort_values(["path", "region_id", "source"]).reset_index(drop=True)


def query(conn: sqlite3.Connection, expression: str) -> pd.DataFrame:
    """Filter :func:`annotated_tubes` with a pandas query expression.

    Example::

        genotype in ['CS', 'w1118'] and interactor == 'DefaultStimulator'
        and stack == 'picamera2'

    Args:
        conn: Census connection.
        expression: A ``DataFrame.query`` expression over the columns of
            :func:`annotated_tubes`.

    Returns:
        The matching tubes.
    """
    return annotated_tubes(conn).query(expression, engine="python")


ETHOSCOPY_COLUMNS = ("machine_name", "date", "time", "region_id", "genotype", "sex", "treatment")


def to_ethoscopy(tubes: pd.DataFrame, missing: str = "unknown") -> pd.DataFrame:
    """Reduce tubes to a metadata table ethoscopy's ``link_meta_index`` accepts.

    ethoscopy rejects any empty cell, so blank descriptive fields become
    ``missing``; ``path`` is left out because ethoscopy adds its own.

    Args:
        tubes: Output of :func:`annotated_tubes` or :func:`query`.
        missing: Placeholder for blank genotype / sex / treatment.

    Returns:
        One row per tube with the :data:`ETHOSCOPY_COLUMNS`.
    """
    table = tubes.loc[:, list(ETHOSCOPY_COLUMNS)].copy()
    table["region_id"] = table["region_id"].astype(int)
    for col in ("genotype", "sex", "treatment"):
        table[col] = table[col].fillna(missing)
    return table.reset_index(drop=True)


def text_summary(conn: sqlite3.Connection) -> str:
    """A short human-readable overview of the census."""
    claims = db_claims(conn)
    found = row_outcomes(conn).drop_duplicates("meta_row_id")
    n_files = conn.execute(
        "SELECT count(DISTINCT path) FROM meta_file WHERE n_rows > 0"
    ).fetchone()[0]
    lines = [
        f"databases indexed: {len(claims)} ({int((claims['readable'] == 0).sum())} unreadable)",
        f"metadata files with rows: {n_files}; rows: {found['meta_row_id'].nunique()}",
        "databases by category:",
    ]
    for category, count in claims["category"].value_counts().sort_index().items():
        lines.append(f"  {category:<10} {count}")
    lines.append("metadata rows by status:")
    for status, count in found["status"].value_counts().sort_index().items():
        lines.append(f"  {status:<15} {count}")
    return "\n".join(lines)


def write_report(conn: sqlite3.Connection, out_dir: str) -> list[str]:
    """Write all report tables as CSV files into ``out_dir``.

    Returns:
        The paths written.
    """
    os.makedirs(out_dir, exist_ok=True)
    tables = {
        "db_claims": db_claims(conn),
        "orphans": None,
        "multiply_claimed": multiply_claimed(conn),
        "unmatched_rows": unmatched_rows(conn),
        "annotated_tubes": annotated_tubes(conn),
    }
    tables["orphans"] = tables["db_claims"].query("category == 'orphan'")
    written = []
    for name, table in tables.items():
        path = os.path.join(out_dir, f"{name}.csv")
        table.to_csv(path, index=False)
        written.append(path)
    for name, table in summaries(conn).items():
        path = os.path.join(out_dir, f"{name}.csv")
        table.to_csv(path)
        written.append(path)
    return written
