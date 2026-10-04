import datetime as dt
import json
import os
import sqlite3

import pytest

from conftest import LEGACY_HW, machine_id_for, make_ethoscope_db, tree_db_path
from ethoscope_metadata_db.census import dbindex


def _by_name(census):
    rows = census.execute("SELECT * FROM db_file").fetchall()
    return {(r["machine_name"], r["path_start"]): r for r in rows}


class TestParsePath:
    def test_standard_layout(self):
        mid = machine_id_for(7)
        rel = f"{mid}/ETHOSCOPE_007/2024-01-05_10-00-00/2024-01-05_10-00-00_{mid}.db"
        info = dbindex.parse_results_path(rel)
        assert info == dbindex.PathInfo(mid, "ETHOSCOPE_007", dt.datetime(2024, 1, 5, 10), True)

    def test_absolute_path_uses_last_components(self):
        mid = machine_id_for(7)
        rel = f"/mnt/results/{mid}/ETHOSCOPE_007/2024-01-05_10-00-00/2024-01-05_10-00-00_{mid}.db"
        assert dbindex.parse_results_path(rel).conforms

    def test_non_standard(self):
        info = dbindex.parse_results_path("misc/copy_of_run.db")
        assert info == dbindex.PathInfo(None, None, None, False)
        mismatched = dbindex.parse_results_path(
            "x/ETHOSCOPE_007/2024-01-05_10-00-00/2024-01-06_10-00-00_y.db"
        )
        assert not mismatched.conforms and mismatched.start == dt.datetime(2024, 1, 6, 10)


class TestReadMetadata:
    def test_picamera2_stimulator_db(self, results_root, census):
        dbindex.scan_results(census, results_root)
        row = _by_name(census)[("ETHOSCOPE_007", "2024-01-05T10:00:00")]
        assert row["readable"] == 1 and row["error"] is None
        assert row["start_utc"] == "2024-01-05T10:00:00"
        assert row["stack"] == "picamera2" and row["camera_sensor"] == "imx708"
        assert row["tracker"] == "AdaptiveBGModel"
        assert row["interactor"] == "DefaultStimulator"
        assert row["duration_h"] == 72
        assert row["n_rois"] == 20 and json.loads(row["roi_ids"]) == list(range(1, 21))
        assert row["gain"] == "1.5" and row["maxfps"] == "15"

    def test_legacy_db_duration_from_roi_rows(self, results_root, census):
        dbindex.scan_results(census, results_root)
        row = _by_name(census)[("ETHOSCOPE_012", "2024-03-25T10:00:00")]
        assert row["stack"] == "legacy" and row["camera_sensor"] == "imx219"
        assert row["duration_h"] == 48 and row["n_rois"] == 10
        assert row["interactor"] == "DefaultInteractor"

    def test_hardware_info_plain_string(self, tmp_path, census):
        root = str(tmp_path / "r")
        start = dt.datetime(2023, 6, 1, 9)
        make_ethoscope_db(
            tree_db_path(root, 3, start),
            machine_number=3,
            start=start,
            hardware_info="unknown hardware",
            selected_options="garbage{",
        )
        dbindex.scan_results(census, root)
        row = census.execute("SELECT * FROM db_file").fetchone()
        assert row["readable"] == 1
        assert row["kernel"] is None and row["stack"] is None and row["tracker"] is None

    def test_camera_as_plain_string_in_dict(self, tmp_path, census):
        root = str(tmp_path / "r")
        start = dt.datetime(2023, 6, 1, 9)
        make_ethoscope_db(
            tree_db_path(root, 3, start), machine_number=3, start=start, hardware_info=LEGACY_HW
        )
        dbindex.scan_results(census, root)
        assert census.execute("SELECT camera_sensor FROM db_file").fetchone()[0] == "imx219"


class TestUnreadable:
    def test_wal_and_corrupt_are_recorded_not_raised(self, results_root, census):
        counts = dbindex.scan_results(census, results_root)
        assert counts == {"seen": 6, "indexed": 6, "unchanged": 0, "unreadable": 2}
        rows = _by_name(census)
        wal = rows[("ETHOSCOPE_012", "2024-04-01T10:00:00")]
        assert wal["readable"] == 0 and "non-empty WAL" in wal["error"]
        corrupt = rows[("ETHOSCOPE_020", "2024-05-01T10:00:00")]
        assert corrupt["readable"] == 0 and "DatabaseError" in corrupt["error"]

    def test_empty_wal_is_read(self, tmp_path, census):
        root = str(tmp_path / "r")
        start = dt.datetime(2023, 6, 1, 9)
        path = make_ethoscope_db(tree_db_path(root, 3, start), machine_number=3, start=start)
        open(path + "-wal", "wb").close()
        dbindex.scan_results(census, root)
        assert census.execute("SELECT readable FROM db_file").fetchone()[0] == 1

    def test_open_readonly_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            dbindex.open_readonly(str(tmp_path / "nope.db"))
        assert not (tmp_path / "nope.db").exists()

    def test_readonly_cannot_write(self, results_root):
        path = next(dbindex.iter_db_files(results_root))
        conn = dbindex.open_readonly(path)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE x (a)")
        conn.close()


class TestRescan:
    def test_unchanged_files_skipped(self, results_root, census):
        dbindex.scan_results(census, results_root)
        counts = dbindex.scan_results(census, results_root)
        # Unreadable files are retried every time, readable ones skipped.
        assert counts["unchanged"] == 4 and counts["indexed"] == 2

    def test_rescan_forces(self, results_root, census):
        dbindex.scan_results(census, results_root)
        assert dbindex.scan_results(census, results_root, rescan=True)["indexed"] == 6

    def test_changed_file_reindexed_with_same_id(self, results_root, census):
        dbindex.scan_results(census, results_root)
        path = next(dbindex.iter_db_files(results_root))
        old = census.execute("SELECT id, mtime FROM db_file WHERE path = ?", (path,)).fetchone()
        os.utime(path, (old["mtime"] + 10, old["mtime"] + 10))
        dbindex.scan_results(census, results_root)
        new = census.execute("SELECT id, mtime FROM db_file WHERE path = ?", (path,)).fetchone()
        assert new["id"] == old["id"] and new["mtime"] == old["mtime"] + 10

    def test_source_files_untouched(self, results_root, census):
        before = {p: os.stat(p).st_mtime for p in dbindex.iter_db_files(results_root)}
        dbindex.scan_results(census, results_root)
        assert {p: os.stat(p).st_mtime for p in before} == before
        assert not any(
            f.endswith(("-shm", "-journal")) for _, _, fs in os.walk(results_root) for f in fs
        )
