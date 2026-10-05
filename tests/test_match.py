import datetime as dt
import os

import pytest

from ethoscope_metadata_db.census import dbindex, discover, match


@pytest.fixture
def populated(results_root, homes_root, census):
    dbindex.scan_results(census, results_root)
    discover.discover(census, homes_root)
    discover.ingest_candidates(census)
    return census


def _outcomes(conn, filename):
    """{row_idx: [(status, method, db path_start, note), ...]} for one file."""
    rows = conn.execute(
        """SELECT r.row_idx, m.status, m.method, d.path_start, m.note
           FROM match m JOIN meta_row r ON r.id = m.meta_row_id
           JOIN meta_file f ON f.id = r.meta_file_id
           LEFT JOIN db_file d ON d.id = m.db_file_id
           WHERE f.path LIKE ? ORDER BY r.row_idx, m.rowid""",
        (f"%{filename}",),
    ).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row[0], []).append(tuple(row[1:]))
    return out


def test_counts(populated):
    # unique: csv rows 1, 2, 4; all three xlsx rows; tsv rows 1, 2, 3, 5.
    # not_found: csv rows 7 (no run *starts* on 7 Jan) and 8; tsv row 4.
    assert match.match_all(populated) == {
        "unique": 10,
        "roi_missing": 2,
        "not_found": 3,
        "incomplete": 1,
    }


def test_csv_outcomes(populated):
    match.match_all(populated)
    out = _outcomes(populated, "metadata.csv")
    # 05/01/2024 is 5 January: dd/mm/yyyy is enforced.
    assert out[1] == [("unique", "start_date", "2024-01-05T10:00:00", None)]
    assert out[3][0][0] == "roi_missing" and "20 regions" in out[3][0][3]
    assert out[4] == [("unique", "start_date", "2024-03-25T10:00:00", None)]
    assert out[5][0][0] == "roi_missing"
    # 07/01 falls inside the run that started 05/01, but only start dates count.
    assert out[7] == [("not_found", None, None, None)]
    assert out[8] == [("not_found", None, None, "no databases for ETHOSCOPE_099")]
    assert out[9] == [("incomplete", None, None, "missing/invalid: date")]


def test_same_day_runs(populated):
    match.match_all(populated)
    out = _outcomes(populated, "timed.tsv")
    # 10/01/2024 is 10 January; no time, so the last run of the day.
    assert out[1][0] == ("unique", "last_of_day", "2024-01-10T15:00:00", "last of 2 runs that day")
    # An exact time picks its run.
    assert out[2] == [("unique", "start_time", "2024-01-10T15:00:00", None)]
    # No time: the last run of the day; the earlier one is recorded as superseded.
    assert out[3] == [
        ("unique", "last_of_day", "2024-01-10T15:00:00", "last of 2 runs that day"),
        ("superseded", "last_of_day", "2024-01-10T09:00:00", "superseded by the 15:00:00 run"),
    ]
    # 12:00 is three hours from both runs.
    assert out[4] == [
        ("not_found", "start_time", None, "no run at 12:00:00 (runs that day: 09:00:00, 15:00:00)")
    ]
    # 14:30 is within tolerance of the 15:00 run only: matched, flagged approximate.
    assert out[5][0][:3] == ("unique", "start_time_approx", "2024-01-10T15:00:00")
    assert "no run exactly at 14:30:00" in out[5][0][3]


def test_rematch_is_idempotent(populated):
    first = match.match_all(populated)
    n = populated.execute("SELECT count(*) FROM match").fetchone()[0]
    assert match.match_all(populated) == first
    assert populated.execute("SELECT count(*) FROM match").fetchone()[0] == n


def _row(**kw):
    base = {
        "date": "2024-01-05",
        "date_alt": None,
        "date_status": "ok",
        "start_time": None,
        "machine_name": "ETHOSCOPE_007",
        "region_id": 1,
    }
    base.update(kw)
    return base


def _cand(id_, start, rois=(1, 2), **kw):
    rois = frozenset(rois) if rois else None
    return match.DbCandidate(id_, f"/{id_}.db", start, rois, **kw)


def _machine(*files):
    return {"ETHOSCOPE_007": match.group_runs(list(files))}


TWO_RUNS = _machine(_cand(1, dt.datetime(2024, 1, 5, 9)), _cand(2, dt.datetime(2024, 1, 5, 10)))


class TestMatchRow:
    def test_exact_time_picks_earlier_run(self):
        [res] = match.match_row(_row(start_time="09:00:00"), TWO_RUNS)
        assert (res.db_file_id, res.status, res.method) == (1, "unique", "start_time")

    def test_time_close_to_several_runs_is_ambiguous(self):
        results = match.match_row(_row(start_time="09:30:00"), TWO_RUNS)
        assert [(r.db_file_id, r.status) for r in results] == [(1, "ambiguous"), (2, "ambiguous")]

    def test_tolerance_is_configurable(self):
        [res] = match.match_row(_row(start_time="09:30:00"), TWO_RUNS, tolerance_min=0)
        assert res.status == "not_found"

    def test_last_of_day_region_missing(self):
        results = match.match_row(_row(region_id=9), TWO_RUNS)
        assert [(r.db_file_id, r.status) for r in results] == [
            (2, "roi_missing"),
            (1, "superseded"),
        ]

    def test_unreadable_candidate_is_roi_unknown(self):
        by_machine = _machine(_cand(1, dt.datetime(2024, 1, 5, 10), rois=None))
        [res] = match.match_row(_row(), by_machine)
        assert (res.db_file_id, res.status) == (1, "roi_unknown")

    def test_running_db_is_not_a_candidate(self):
        by_machine = _machine(_cand(1, dt.datetime(2024, 1, 3, 10)))
        [res] = match.match_row(_row(), by_machine)
        assert res.status == "not_found" and res.db_file_id is None

    def test_invalid_date_and_missing_region(self):
        [res] = match.match_row(_row(date=None, date_status="invalid", region_id=None), {})
        assert res.status == "incomplete" and res.note == "missing/invalid: date, region_id"

    def test_load_candidates(self, results_root, census):
        dbindex.scan_results(census, results_root)
        by_machine = match.load_candidates(census)
        assert sorted(by_machine) == ["ETHOSCOPE_007", "ETHOSCOPE_012", "ETHOSCOPE_020"]
        assert [r.start.hour for r in by_machine["ETHOSCOPE_007"]] == [10, 9, 15]
        assert by_machine["ETHOSCOPE_020"][0].files[0].roi_ids is None

    def test_start_is_metadata_on_the_lab_clock(self, tmp_path, census):
        from conftest import make_ethoscope_db

        # Stamp says 2020-10-03 (a node bug), METADATA says 2022-09-27 08:30 UTC = 09:30 BST.
        root = str(tmp_path / "r")
        mid = "147" + "c" * 29
        stamp = "2020-10-03_19-15-27"
        path = os.path.join(root, mid, "ETHOSCOPE_147", stamp, f"{stamp}_{mid}.db")
        make_ethoscope_db(path, machine_number=147, start=dt.datetime(2022, 9, 27, 8, 30))
        dbindex.scan_results(census, root)
        [run] = match.load_candidates(census)["ETHOSCOPE_147"]
        assert run.start == dt.datetime(2022, 9, 27, 9, 30)
        assert run.files[0].stamp == dt.datetime(2020, 10, 3, 19, 15, 27)
        assert match.load_candidates(census, "UTC")["ETHOSCOPE_147"][0].start.hour == 8

    def test_wrong_stamp_found_by_metadata_date(self):
        by_machine = _machine(
            _cand(1, dt.datetime(2022, 9, 27, 9, 30), stamp=dt.datetime(2020, 10, 3, 19, 15))
        )
        [res] = match.match_row(_row(date="2022-09-27"), by_machine)
        assert (res.status, res.method) == ("unique", "start_date")

    def test_path_date_is_a_flagged_fallback(self):
        by_machine = _machine(
            _cand(1, dt.datetime(2022, 9, 27, 9, 30), stamp=dt.datetime(2020, 10, 3, 19, 15))
        )
        [res] = match.match_row(_row(date="2020-10-03"), by_machine)
        assert (res.status, res.method) == ("unique", "start_date+path_date")
        assert "METADATA start 2022-09-27 09:30:00" in res.note

    def test_time_may_be_the_path_stamp(self):
        # A UTC stamp in summer is an hour behind the lab clock; users copy the stamp.
        by_machine = _machine(
            _cand(1, dt.datetime(2024, 6, 5, 9), stamp=dt.datetime(2024, 6, 5, 8)),
            _cand(2, dt.datetime(2024, 6, 5, 15), stamp=dt.datetime(2024, 6, 5, 14)),
        )
        [res] = match.match_row(_row(date="2024-06-05", start_time="08:00:00"), by_machine)
        assert (res.db_file_id, res.method) == (1, "start_time")


class TestCopies:
    START = dt.datetime(2024, 1, 5, 10)

    def test_standard_name_is_used_and_copies_linked(self):
        by_machine = _machine(_cand(1, self.START, standard=False), _cand(2, self.START))
        results = match.match_row(_row(), by_machine)
        assert [(r.db_file_id, r.status) for r in results] == [(2, "unique"), (1, "duplicate")]
        assert "2 files of one run" in results[0].note

    def test_largest_standard_copy_is_used(self):
        # A partial copy of the same run (same start to the second) under another folder.
        by_machine = _machine(_cand(1, self.START, size=10), _cand(2, self.START, size=99))
        results = match.match_row(_row(), by_machine)
        assert [(r.db_file_id, r.status) for r in results] == [(2, "unique"), (1, "duplicate")]
        assert results[0].note == "2 files of one run: largest used"

    def test_no_readable_standard_copy_is_ambiguous(self):
        by_machine = _machine(_cand(1, self.START, standard=False), _cand(2, self.START, rois=None))
        results = match.match_row(_row(), by_machine)
        assert [(r.db_file_id, r.status) for r in results] == [(1, "ambiguous"), (2, "ambiguous")]

    def test_empty_file_is_never_last_of_day(self):
        by_machine = _machine(
            _cand(1, dt.datetime(2024, 1, 5, 10)),
            _cand(2, dt.datetime(2024, 1, 5, 18), rois=None, size=0),
        )
        results = match.match_row(_row(), by_machine)
        assert [(r.db_file_id, r.status) for r in results] == [(1, "unique"), (2, "superseded")]


class TestDateOrder:
    def test_month_first_reading_finding_a_run_is_ambiguous(self):
        by_machine = _machine(
            _cand(1, dt.datetime(2024, 1, 5, 10)), _cand(2, dt.datetime(2024, 5, 1, 10))
        )
        row = _row(date="2024-01-05", date_alt="2024-05-01", date_status="ambiguous")
        results = match.match_row(row, by_machine)
        assert [(r.db_file_id, r.status) for r in results] == [
            (1, "date_ambiguous"),
            (2, "date_ambiguous"),
        ]
        assert results[0].note == "day/month order unknown: 2024-01-05 or 2024-05-01"

    def test_only_one_reading_finds_a_run(self):
        by_machine = _machine(_cand(1, dt.datetime(2024, 1, 5, 10)))
        row = _row(date="2024-01-05", date_alt="2024-05-01", date_status="ambiguous")
        [res] = match.match_row(row, by_machine)
        assert (res.db_file_id, res.status) == (1, "unique")
        # Only the month-first reading finds a run: still flagged, never silent.
        row = _row(date="2024-05-01", date_alt="2024-01-05", date_status="ambiguous")
        [res] = match.match_row(row, by_machine)
        assert (res.db_file_id, res.status) == (1, "date_ambiguous")
