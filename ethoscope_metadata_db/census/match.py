"""Match metadata rows to ethoscope databases, never silently.

The metadata date identifies a run: it is always the date the experiment
*started*. A run's start is the ``date_time`` in its ``METADATA``, on the
lab's wall clock (``Europe/London`` by default); the results-tree stamp is
used only when METADATA cannot be read, because some stamps are wrong (a
node bug wrote stale dates) while users' metadata follows METADATA. Files of
one machine with the same start are copies of one run; the readable one
with the standard name is used, the largest if several.

Candidates are the runs on the row's (normalised) machine starting that day.
When several runs start the same day (false starts, early crashes) the real
experiment is the last of the day; a ``time`` column picks a run explicitly.
Every outcome is stored with an explicit status:

``unique``          one database, region present in its ROI_MAP
``roi_missing``     one database, but the region is not in its ROI_MAP
``roi_unknown``     one database, unreadable, so the region cannot be checked
``superseded``      an earlier same-day run passed over for the last one
                    (stored alongside the row's main result)
``duplicate``       another file of the chosen run (stored alongside)
``ambiguous``       a ``time`` close to several runs, or several files of one
                    run none of which is readable with the standard name
``date_ambiguous``  the date reads as two days (day/month order unknown) and
                    a run starts on the month-first one (one row per candidate)
``not_found``       no run on that machine starts on that date (and time)
``incomplete``      the row lacks a usable date, machine or region

A method ending in ``+path_date`` means no run started that day by METADATA
and the run was found by its results-tree stamp instead.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .normalise import normalise_machine_name

DEFAULT_TIME_TOLERANCE_MIN = 90.0
DEFAULT_TIMEZONE = "Europe/London"
USABLE_DATE_STATUSES = ("ok", "ambiguous")
# Extra links stored next to a row's result; they never make a row's status.
LINK_STATUSES = ("superseded", "duplicate")


@dataclass(frozen=True)
class DbCandidate:
    """The facts about one database file that matching needs.

    Attributes:
        id: ``db_file.id``.
        path: The file.
        start: Start on the lab's wall clock (METADATA; the stamp if unreadable).
        roi_ids: Regions in ROI_MAP, ``None`` when the file is unreadable.
        stamp: Start written in the results-tree path, if any.
        standard: Whether the path follows the standard layout.
        size: File size in bytes (0: an empty file).
    """

    id: int
    path: str
    start: dt.datetime
    roi_ids: frozenset[int] | None
    stamp: dt.datetime | None = None
    standard: bool = True
    size: int = 1

    @property
    def empty(self) -> bool:
        return self.size == 0


@dataclass(frozen=True)
class Run:
    """One recording: the files of one machine that share a start."""

    start: dt.datetime
    files: tuple[DbCandidate, ...]

    @property
    def start_date(self) -> dt.date:
        return self.start.date()

    @property
    def empty(self) -> bool:
        return all(f.empty for f in self.files)

    def stamps(self) -> list[dt.datetime]:
        return [f.stamp for f in self.files if f.stamp is not None]

    def seconds_from(self, moment: dt.datetime) -> float:
        """Distance of ``moment`` from the start or from any stamp of the run."""
        return min(abs((t - moment).total_seconds()) for t in [self.start, *self.stamps()])


@dataclass(frozen=True)
class MatchResult:
    """One ``match`` row to store (``db_file_id`` None when no database)."""

    db_file_id: int | None
    status: str
    method: str | None
    note: str | None = None


def group_runs(files: list[DbCandidate]) -> list[Run]:
    """Group one machine's files into runs (same start), sorted by start."""
    by_start: dict[dt.datetime, list[DbCandidate]] = defaultdict(list)
    for candidate in files:
        by_start[candidate.start].append(candidate)
    return [
        Run(start, tuple(sorted(group, key=lambda c: c.path)))
        for start, group in sorted(by_start.items())
    ]


def _local(utc_iso: str, zone: ZoneInfo) -> dt.datetime:
    moment = dt.datetime.fromisoformat(utc_iso).replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(zone).replace(tzinfo=None)


def load_candidates(
    conn: sqlite3.Connection, timezone: str = DEFAULT_TIMEZONE
) -> dict[str, list[Run]]:
    """Group indexed databases into runs per canonical machine name.

    Databases with no usable start are left out (they cannot be matched by
    date and show up as orphans in reports).

    Args:
        conn: Census connection.
        timezone: The lab's time zone, in which users write dates and times.
    """
    zone = ZoneInfo(timezone)
    files: dict[str, list[DbCandidate]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM db_file"):
        stamp = dt.datetime.fromisoformat(row["path_start"]) if row["path_start"] else None
        start = _local(row["start_utc"], zone) if row["start_utc"] else stamp
        machine = normalise_machine_name(row["machine_name"])
        if start is None or machine is None:
            continue
        rois = frozenset(json.loads(row["roi_ids"])) if row["readable"] and row["roi_ids"] else None
        files[machine].append(
            DbCandidate(
                row["id"],
                row["path"],
                start,
                rois,
                stamp=stamp,
                standard="non-standard path" not in (row["error"] or ""),
                size=row["size"] or 0,
            )
        )
    return {machine: group_runs(group) for machine, group in files.items()}


def _hms(moment: dt.datetime) -> str:
    return moment.strftime("%H:%M:%S")


def _by_time(
    same_day: list[Run], day: dt.date, start_time: str, tolerance_min: float
) -> tuple[list[Run], str, str | None]:
    """Select same-day runs by the row's ``time``.

    The time may be the run's local start or its path stamp (what ethoscopy
    matches). An exact match wins; otherwise runs within ``tolerance_min``
    are kept and the method says the match is approximate.

    Returns:
        ``(selected, method, note)``.
    """
    exact = [r for r in same_day if start_time in {_hms(t) for t in [r.start, *r.stamps()]}]
    if exact:
        return exact, "start_time", None
    wanted = dt.datetime.combine(day, dt.time.fromisoformat(start_time))
    close = [r for r in same_day if r.seconds_from(wanted) <= tolerance_min * 60]
    runs = ", ".join(_hms(r.start) for r in same_day)
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


def _run_results(run: Run, region_id: int, method: str, notes: list[str]) -> list[MatchResult]:
    """Results for a selected run: its file, plus links to any copies.

    Copies share the start to the second, so they hold the same recording:
    the readable one with the standard name is used, the largest (most
    complete) if there are several. With no readable standard copy the run
    is ``ambiguous``.
    """
    if len(run.files) == 1:
        return [_roi_result(run.files[0], region_id, method, notes)]
    preferred = [f for f in run.files if f.standard and f.roi_ids is not None]
    if not preferred:
        note = "; ".join([*notes, f"{len(run.files)} files of one run, none readable and standard"])
        return [MatchResult(f.id, "ambiguous", method, note) for f in run.files]
    chosen = max(preferred, key=lambda f: f.size)  # first by path on equal sizes
    how = "standard name used" if len(preferred) == 1 else "largest used"
    notes = [*notes, f"{len(run.files)} files of one run: {how}"]
    copies = [
        MatchResult(f.id, "duplicate", method, f"copy of {chosen.path}")
        for f in run.files
        if f is not chosen
    ]
    return [_roi_result(chosen, region_id, method, notes), *copies]


def _match_day(
    runs: list[Run], day: dt.date, start_time: str | None, region_id: int, tolerance_min: float
) -> list[MatchResult]:
    """Match a row to the runs of its machine for one reading of its date."""
    notes: list[str] = []
    suffix = ""
    same_day = [r for r in runs if r.start_date == day]
    if not same_day:
        same_day = [r for r in runs if day in {s.date() for s in r.stamps()}]
        if not same_day:
            return [MatchResult(None, "not_found", None, None)]
        suffix = "+path_date"
        starts = ", ".join(r.start.isoformat(" ", "seconds") for r in same_day)
        notes.append(f"found by path date; METADATA start {starts}")

    if start_time:
        selected, method, time_note = _by_time(same_day, day, start_time, tolerance_min)
        notes += [time_note] if time_note else []
        if not selected:
            return [MatchResult(None, "not_found", method + suffix, "; ".join(notes))]
        if len(selected) > 1:
            note = "; ".join(notes)
            return [
                MatchResult(f.id, "ambiguous", method + suffix, note)
                for run in selected
                for f in run.files
            ]
        return _run_results(selected[0], region_id, method + suffix, notes)

    if len(same_day) == 1:
        return _run_results(same_day[0], region_id, "start_date" + suffix, notes)
    # Reason: empty files are failed starts, never the experiment, unless nothing else ran.
    last = ([r for r in same_day if not r.empty] or same_day)[-1]
    notes.append(f"last of {len(same_day)} runs that day")
    main = _run_results(last, region_id, "last_of_day" + suffix, notes)
    note = f"superseded by the {_hms(last.start)} run"
    passed_over = [
        MatchResult(f.id, "superseded", "last_of_day" + suffix, note)
        for run in same_day
        if run is not last
        for f in run.files
    ]
    return main + passed_over


def _found(results: list[MatchResult]) -> list[MatchResult]:
    return [r for r in results if r.db_file_id is not None and r.status not in LINK_STATUSES]


def match_row(
    row: sqlite3.Row,
    by_machine: dict[str, list[Run]],
    tolerance_min: float = DEFAULT_TIME_TOLERANCE_MIN,
) -> list[MatchResult]:
    """Match one ``meta_row`` and return the ``match`` rows to store.

    Args:
        row: A ``meta_row`` record.
        by_machine: Output of :func:`load_candidates`.
        tolerance_min: How far a ``time`` may be from a run's start when no
            run matches it exactly, in minutes.

    Returns:
        The row's main result first (its status is the row's status),
        followed by any further candidates, copies or ``superseded`` runs.
    """
    missing = [
        name
        for name, ok in (
            ("date", row["date_status"] in USABLE_DATE_STATUSES),
            ("machine_name", row["machine_name"] is not None),
            ("region_id", row["region_id"] is not None),
        )
        if not ok
    ]
    if missing:
        return [MatchResult(None, "incomplete", None, "missing/invalid: " + ", ".join(missing))]

    runs = by_machine.get(row["machine_name"], [])
    if not runs:
        return [MatchResult(None, "not_found", None, f"no databases for {row['machine_name']}")]

    def on(day: str) -> list[MatchResult]:
        return _match_day(
            runs, dt.date.fromisoformat(day), row["start_time"], row["region_id"], tolerance_min
        )

    results = on(row["date"])
    if row["date_alt"]:
        month_first = _found(on(row["date_alt"]))
        if month_first:
            note = f"day/month order unknown: {row['date']} or {row['date_alt']}"
            return [
                dataclasses.replace(r, status="date_ambiguous", note=note)
                for r in _found(results) + month_first
            ]
    return results


def match_all(
    conn: sqlite3.Connection,
    tolerance_min: float = DEFAULT_TIME_TOLERANCE_MIN,
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, int]:
    """Recompute all matches from scratch.

    Matching is cheap compared with scanning, so it is always a full,
    deterministic recomputation.

    Args:
        conn: Census connection.
        tolerance_min: See :func:`match_row`.
        timezone: See :func:`load_candidates`.

    Returns:
        Number of metadata rows per status (each row counted once, by its
        main result).
    """
    by_machine = load_candidates(conn, timezone)
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
