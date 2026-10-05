"""Persist the census views a user interface needs, as indexed tables.

``report.db_claims`` and the row outcomes are built from the full match join,
which takes seconds on a large census: fine once after each ``match``, too
slow for every web request. ``summarise`` stores their results as two tables,
rebuilt in full each time:

``db_summary``    one row per database: recording facts, its local start date,
                  claim category and claim counts.
``file_summary``  one row per metadata sheet: provenance, location tag, row
                  outcomes by status, date range and databases claimed.
``genotype_summary`` each distinct raw genotype string, with row and file counts.

Each table is written under a temporary name and swapped in within one
transaction, so readers never see it half-built.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from . import report
from .match import DEFAULT_TIMEZONE

ROW_STATUSES = (
    "unique",
    "not_found",
    "roi_missing",
    "roi_unknown",
    "date_ambiguous",
    "ambiguous",
    "incomplete",
)

DB_COLUMNS = (
    "id",
    "path",
    "machine_name",
    "machine_id",
    "path_start",
    "start_utc",
    "start_local",
    "month",
    "year",
    "experimenter",
    "location",
    "stack",
    "kernel",
    "camera_sensor",
    "pi_version",
    "tracker",
    "interactor",
    "sw_version",
    "n_rois",
    "size",
    "duration_h",
    "readable",
    "error",
    "category",
    "n_rows",
    "n_sources",
    "homes",
    "n_tentative",
    "n_superseded",
    "n_duplicate",
)
DB_INDEXES = (
    "category",
    "month",
    "machine_name",
    "experimenter",
    "stack",
    "interactor",
    "location",
)
FILE_INDEXES = ("tag", "home", "status", "sha256", "outcome", "year")


def tag_for(path: str, tags: Iterable[tuple[str, str]]) -> str | None:
    """Location tag of ``path``: the tag of the longest matching root prefix."""
    best, best_len = None, -1
    for prefix, tag in tags:
        root = prefix.rstrip("/") + "/"
        if path.startswith(root) and len(root) > best_len:
            best, best_len = tag, len(root)
    return best


def _local_start(row: pd.Series, zone: ZoneInfo) -> str | None:
    if isinstance(row["start_utc"], str) and row["start_utc"]:
        moment = dt.datetime.fromisoformat(row["start_utc"]).replace(tzinfo=dt.timezone.utc)
        return moment.astimezone(zone).replace(tzinfo=None).isoformat(timespec="seconds")
    return row["path_start"] if isinstance(row["path_start"], str) else None


def db_summary(conn: sqlite3.Connection, timezone: str = DEFAULT_TIMEZONE) -> pd.DataFrame:
    """One row per database with its claim category (see :func:`report.db_claims`).

    ``start_local`` is the METADATA start on the lab clock (the folder stamp
    when METADATA is unreadable), the same date matching uses; ``month`` and
    ``year`` derive from it.
    """
    claims = report.db_claims(conn)
    if claims.empty:
        return claims.reindex(columns=list(DB_COLUMNS))
    zone = ZoneInfo(timezone)
    claims["start_local"] = claims.apply(lambda r: _local_start(r, zone), axis=1)
    claims["month"] = claims["start_local"].str[:7]
    claims["year"] = claims["start_local"].str[:4]
    return claims.loc[:, list(DB_COLUMNS)]


def file_summary(conn: sqlite3.Connection, tags: Iterable[tuple[str, str]] = ()) -> pd.DataFrame:
    """One row per metadata sheet with its row outcomes and the databases it claims."""
    tags = list(tags)
    files = pd.read_sql_query(
        "SELECT id, path, sheet, home, status, n_rows, sha256, size, mtime, date_order, note "
        "FROM meta_file",
        conn,
    )
    files["tag"] = files["path"].map(lambda p: tag_for(p, tags))
    links = pd.read_sql_query(
        "SELECT r.meta_file_id AS id, r.id AS row_id, r.date, m.status, m.db_file_id "
        "FROM match m JOIN meta_row r ON r.id = m.meta_row_id "
        f"WHERE m.status NOT IN ({', '.join('?' * len(report.LINK_STATUSES))})",
        conn,
        params=list(report.LINK_STATUSES),
    )
    distinct = links.drop_duplicates(["row_id", "status"])
    counts = pd.crosstab(distinct["id"], distinct["status"]) if len(distinct) else pd.DataFrame()
    counts = counts.reindex(columns=list(ROW_STATUSES), fill_value=0)
    counts.columns = [f"n_{c}" for c in counts.columns]
    claimed = links[links["status"].isin(report.CLAIM_STATUSES)]
    per_file = links.groupby("id").agg(date_min=("date", "min"), date_max=("date", "max"))
    per_file["n_dbs"] = claimed.groupby("id")["db_file_id"].nunique()
    out = files.merge(counts, left_on="id", right_index=True, how="left")
    out = out.merge(per_file, left_on="id", right_index=True, how="left")
    number_columns = [*counts.columns, "n_dbs"]
    out[number_columns] = out[number_columns].fillna(0).astype(int)
    accepted = out["status"].isin(report.ACCEPTED_SHEET_STATUSES)
    copies = out[accepted].groupby("sha256")["id"].transform("size")
    out["n_copies"] = copies.reindex(out.index).fillna(0).astype(int)
    out["outcome"] = _outcome(out, accepted)
    out["year"] = out["date_min"].str[:4]
    return out


def _outcome(files: pd.DataFrame, accepted: pd.Series) -> pd.Series:
    """One word per sheet for browsing: how well its rows matched."""
    unmatched = files[[f"n_{s}" for s in ROW_STATUSES if s != "unique"]].sum(axis=1)
    outcome = pd.Series("not metadata", index=files.index)
    outcome[accepted & (files["n_rows"].fillna(0) == 0)] = "empty"
    rows = accepted & (files["n_rows"].fillna(0) > 0)
    outcome[rows & (unmatched == 0)] = "all matched"
    outcome[rows & (unmatched > 0) & (files["n_unique"] > 0)] = "some unmatched"
    outcome[rows & (files["n_unique"] == 0)] = "none matched"
    return outcome


def genotype_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    """Each distinct raw genotype string with how many rows and files use it."""
    frame = pd.read_sql_query(
        "SELECT genotype_raw AS genotype, count(*) AS n_rows, "
        "count(DISTINCT meta_file_id) AS n_files FROM meta_row "
        "WHERE genotype_raw IS NOT NULL GROUP BY genotype_raw ORDER BY n_rows DESC",
        conn,
    )
    frame.insert(0, "id", range(1, len(frame) + 1))
    return frame


def _replace_table(
    conn: sqlite3.Connection, name: str, frame: pd.DataFrame, indexes: Iterable[str]
) -> None:
    staging = f"{name}_new"
    conn.execute(f"DROP TABLE IF EXISTS {staging}")
    frame.to_sql(staging, conn, index=False)
    with conn:
        conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.execute(f"ALTER TABLE {staging} RENAME TO {name}")
        conn.execute(f"CREATE UNIQUE INDEX {name}_id ON {name} (id)")
        for column in indexes:
            conn.execute(f"CREATE INDEX {name}_{column} ON {name} ({column})")


def summarise(
    conn: sqlite3.Connection,
    tags: Iterable[tuple[str, str]] = (),
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, int]:
    """Rebuild ``db_summary`` and ``file_summary``.

    Args:
        conn: Census connection (after ``match``).
        tags: ``(root path, location tag)`` pairs naming where metadata files
            come from (homes, alumni, ...).
        timezone: The lab's time zone, as used by matching.

    Returns:
        Row counts of the tables.
    """
    dbs = db_summary(conn, timezone)
    files = file_summary(conn, tags)
    genotypes = genotype_summary(conn)
    _replace_table(conn, "db_summary", dbs, DB_INDEXES)
    _replace_table(conn, "file_summary", files, FILE_INDEXES)
    _replace_table(conn, "genotype_summary", genotypes, ("genotype",))
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES ('summarised_at', ?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),),
        )
    return {"db_summary": len(dbs), "file_summary": len(files), "genotype_summary": len(genotypes)}
