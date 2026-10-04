"""Match metadata rows to ethoscope databases, never silently.

The metadata date identifies a database: it is always the date the
experiment *started*, i.e. the date in the results-tree stamp. Candidates are
therefore the databases on the same (normalised) machine starting that day.
When several runs start the same day (false starts, early crashes) the real
experiment is the last of the day; a ``time`` column picks a run explicitly.
Every outcome is stored with an explicit status:

``unique``          one database, region present in its ROI_MAP
``roi_missing``     one database, but the region is not in its ROI_MAP
``roi_unknown``     one database, unreadable, so the region cannot be checked
``superseded``      an earlier same-day run passed over for the last one
                    (stored alongside the row's main result)
``ambiguous``       a ``time`` close to several runs (one ``match`` row each)
``not_found``       no database on that machine starts on that date (and time)
``incomplete``      the row lacks a usable date, machine or region
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from .normalise import normalise_machine_name

DEFAULT_TIME_TOLERANCE_MIN = 90.0


@dataclass(frozen=True)
class DbCandidate:
    """The facts about one database that matching needs."""

    id: int
    path: str
    start: dt.datetime
    roi_ids: frozenset[int] | None

    @property
    def start_date(self) -> dt.date:
        return self.start.date()


@dataclass(frozen=True)
class MatchResult:
    """One ``match`` row to store (``db_file_id`` None when no database)."""

    db_file_id: int | None
    status: str
    method: str | None
    note: str | None = None


def _db_start(row: sqlite3.Row) -> dt.datetime | None:
    # The directory stamp is what users write in their metadata (and what
    # ethoscopy globs on); METADATA's UTC start is only a fallback.
    for key in ("path_start", "start_utc"):
        if row[key]:
            return dt.datetime.fromisoformat(row[key])
    return None


def load_candidates(conn: sqlite3.Connection) -> dict[str, list[DbCandidate]]:
    """Group indexed databases by canonical machine name, sorted by start.

    Databases with no usable start time are left out (they cannot be matched
    by date and show up as orphans in reports).
    """
    by_machine: dict[str, list[DbCandidate]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM db_file"):
        start = _db_start(row)
        machine = normalise_machine_name(row["machine_name"])
        if start is None or machine is None:
            continue
        rois = frozenset(json.loads(row["roi_ids"])) if row["readable"] and row["roi_ids"] else None
        by_machine[machine].append(DbCandidate(row["id"], row["path"], start, rois))
    for candidates in by_machine.values():
        candidates.sort(key=lambda c: (c.start, c.path))
    return by_machine


def _starting_on(candidates: list[DbCandidate], day: dt.date) -> list[DbCandidate]:
    return [c for c in candidates if c.start_date == day]


def _hms(moment: dt.datetime) -> str:
    return moment.strftime("%H:%M:%S")


def _by_time(
    same_day: list[DbCandidate], start_time: str, tolerance_min: float
) -> tuple[list[DbCandidate], str, str | None]:
    """Select same-day runs by the row's ``time``.

    An exact match on the stamp wins. Otherwise runs within
    ``tolerance_min`` are kept, and the method says the match is approximate.

    Returns:
        ``(selected, method, note)``.
    """
    exact = [c for c in same_day if _hms(c.start) == start_time]
    if exact:
        return exact, "start_time", None
    wanted = dt.datetime.combine(same_day[0].start_date, dt.time.fromisoformat(start_time))
    close = [c for c in same_day if abs((c.start - wanted).total_seconds()) <= tolerance_min * 60]
    runs = ", ".join(_hms(c.start) for c in same_day)
    if not close:
        return [], "start_time", f"no run at {start_time} (runs that day: {runs})"
    return close, "start_time_approx", f"no run exactly at {start_time} (runs that day: {runs})"


def _roi_result(candidate: DbCandidate, region_id: int, method: str, notes: list[str]):
    if candidate.roi_ids is None:
        status = "roi_unknown"
        notes.append("database unreadable")
    elif region_id in candidate.roi_ids:
        status = "unique"
    else:
        status = "roi_missing"
        notes.append(f"ROI_MAP has {len(candidate.roi_ids)} regions")
    return MatchResult(candidate.id, status, method, "; ".join(notes) or None)


def match_row(
    row: sqlite3.Row,
    by_machine: dict[str, list[DbCandidate]],
    tolerance_min: float = DEFAULT_TIME_TOLERANCE_MIN,
) -> list[MatchResult]:
    """Match one ``meta_row`` and return the ``match`` rows to store.

    Args:
        row: A ``meta_row`` record.
        by_machine: Output of :func:`load_candidates`.
        tolerance_min: How far a ``time`` may be from a run's stamp when no
            run matches it exactly, in minutes.

    Returns:
        The row's main result first (its status is the row's status),
        followed by any further candidates or ``superseded`` runs.
    """
    missing = [
        name
        for name, ok in (
            ("date", row["date_status"] == "ok"),
            ("machine_name", row["machine_name"] is not None),
            ("region_id", row["region_id"] is not None),
        )
        if not ok
    ]
    if missing:
        return [MatchResult(None, "incomplete", None, "missing/invalid: " + ", ".join(missing))]

    machine_dbs = by_machine.get(row["machine_name"], [])
    same_day = _starting_on(machine_dbs, dt.date.fromisoformat(row["date"]))
    if not same_day:
        note = None if machine_dbs else f"no databases for {row['machine_name']}"
        return [MatchResult(None, "not_found", None, note)]

    notes: list[str] = []
    if row["start_time"]:
        selected, method, time_note = _by_time(same_day, row["start_time"], tolerance_min)
        notes += [time_note] if time_note else []
        if not selected:
            return [MatchResult(None, "not_found", method, "; ".join(notes))]
        if len(selected) > 1:
            note = "; ".join(notes)
            return [MatchResult(c.id, "ambiguous", method, note) for c in selected]
        return [_roi_result(selected[0], row["region_id"], method, notes)]

    if len(same_day) == 1:
        return [_roi_result(same_day[0], row["region_id"], "start_date", notes)]
    *earlier, last = same_day
    notes.append(f"last of {len(same_day)} runs that day")
    main = _roi_result(last, row["region_id"], "last_of_day", notes)
    note = f"superseded by the {_hms(last.start)} run"
    return [main] + [MatchResult(c.id, "superseded", "last_of_day", note) for c in earlier]


def match_all(
    conn: sqlite3.Connection, tolerance_min: float = DEFAULT_TIME_TOLERANCE_MIN
) -> dict[str, int]:
    """Recompute all matches from scratch.

    Matching is cheap compared with scanning, so it is always a full,
    deterministic recomputation.

    Args:
        conn: Census connection.
        tolerance_min: See :func:`match_row`.

    Returns:
        Number of metadata rows per status (each row counted once, by its
        main result).
    """
    by_machine = load_candidates(conn)
    conn.execute("DELETE FROM match")
    counts: dict[str, int] = defaultdict(int)
    batch = []
    for row in conn.execute("SELECT * FROM meta_row ORDER BY id"):
        results = match_row(row, by_machine, tolerance_min)
        counts[results[0].status] += 1
        batch += [(row["id"], r.db_file_id, r.status, r.method, r.note) for r in results]
    conn.executemany(
        "INSERT INTO match (meta_row_id, db_file_id, status, method, note) VALUES (?, ?, ?, ?, ?)",
        batch,
    )
    conn.commit()
    return dict(counts)
