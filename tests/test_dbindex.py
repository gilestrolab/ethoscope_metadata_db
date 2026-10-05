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
        assert row["experimenter"] == "alice" and row["location"] == "Incubator_1"

    def test_legacy_db_without_stop_has_no_duration(self, results_root, census):
        dbindex.scan_results(census, results_root)
        row = _by_name(census)[("ETHOSCOPE_012", "2024-03-25T10:00:00")]
        assert row["stack"] == "legacy" and row["camera_sensor"] == "imx219"
        assert row["duration_h"] is None and row["n_rois"] == 10
        assert row["interactor"] == "DefaultInteractor"

    def test_tracking_tables_are_never_read(self, results_root):
        path = next(p for p in dbindex.iter_db_files(results_root) if "ETHOSCOPE_012" in p)
        conn = dbindex.open_readonly(path)
        read_tables = set()

        def record(action, table, *_):
            if action == sqlite3.SQLITE_READ:
                read_tables.add(table)
            return sqlite3.SQLITE_OK

        conn.set_authorizer(record)
        dbindex.read_db_metadata(conn)
        conn.close()
        assert "METADATA" in read_tables and "ROI_MAP" in read_tables
        assert not [t for t in read_tables if t.startswith("ROI_") and t != "ROI_MAP"]

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

    def test_structured_pi_version_stored_as_json(self, tmp_path, census):
        root = str(tmp_path / "r")
        start = dt.datetime(2025, 9, 24, 18)
        hardware = {**LEGACY_HW, "pi_version": {"model_number": 3, "model_type": "Model B Rev"}}
        make_ethoscope_db(
            tree_db_path(root, 11, start), machine_number=11, start=start, hardware_info=hardware
        )
        assert dbindex.scan_results(census, root)["unreadable"] == 0
        row = census.execute("SELECT pi_version, kernel FROM db_file").fetchone()
        assert json.loads(row["pi_version"]) == {"model_number": 3, "model_type": "Model B Rev"}
        assert row["kernel"] == LEGACY_HW["kernel"]

    def test_blank_experimental_info(self, tmp_path, census):
        root = str(tmp_path / "r")
        start = dt.datetime(2017, 6, 1, 9)
        make_ethoscope_db(
            tree_db_path(root, 3, start),
            machine_number=3,
            start=start,
            experimental_info={"name": " ", "location": "", "code": ""},
        )
        dbindex.scan_results(census, root)
        row = census.execute("SELECT experimenter, location FROM db_file").fetchone()
        assert row["experimenter"] is None and row["location"] is None

    def test_new_firmware_options(self, tmp_path, census):
        root = str(tmp_path / "r")
        start = dt.datetime(2026, 9, 1, 9)
        make_ethoscope_db(
            tree_db_path(root, 354, start),
            machine_number=354,
            start=start,
            selected_options=NEW_FIRMWARE_OPTIONS,
        )
        dbindex.scan_results(census, root)
        row = census.execute(
            "SELECT tracker, interactor, interactor_kwargs FROM db_file"
        ).fetchone()
        assert row["interactor"] == "ComposedStimulator" and row["tracker"] == "AdaptiveBGModel"
        assert json.loads(row["interactor_kwargs"]) == {"trigger_type": "inactivity"}

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


class TestFileSelection:
    def test_hidden_files_skipped(self, results_root, census):
        real = next(dbindex.iter_db_files(results_root))
        folder, name = os.path.split(real)
        with open(os.path.join(folder, "." + name), "wb") as handle:  # rsync temporary
            handle.write(b"partial")
        assert dbindex.scan_results(census, results_root)["seen"] == 6

    def test_listed_paths_only(self, results_root, census, caplog):
        every = list(dbindex.iter_db_files(results_root))
        listed = [os.path.relpath(every[0], results_root), every[1], "missing/x.db", "notes.txt"]
        counts = dbindex.scan_results(census, results_root, paths=listed)
        assert counts["seen"] == 2
        paths = {r[0] for r in census.execute("SELECT path FROM db_file")}
        assert paths == {every[0], every[1]}
        assert "listed database not found" in caplog.text

    def test_empty_list_scans_nothing(self, results_root, census):
        assert dbindex.scan_results(census, results_root, paths=[])["seen"] == 0


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


NEW_FIRMWARE_OPTIONS = (
    "OrderedDict({'interactor': {'possible_classes': ["
    "<class 'ethoscope.stimulators.stimulators.DefaultStimulator'>, "
    "<class 'ethoscope.stimulators.composed_stimulator.ComposedStimulator'>], "
    "'class': <class 'ethoscope.stimulators.composed_stimulator.ComposedStimulator'>, "
    "'kwargs': {'trigger_type': 'inactivity'}}, "
    "'tracker': {'class': <class 'ethoscope.trackers.adaptive_bg_tracker.AdaptiveBGModel'>, "
    "'kwargs': {}}})"
)


class TestParseOptions:
    def test_ordered_dict_with_class_reprs(self):
        options = dbindex.parse_options(NEW_FIRMWARE_OPTIONS)
        assert dbindex._option_name(options, "interactor") == (
            "ComposedStimulator",
            {"trigger_type": "inactivity"},
        )

    def test_ordered_dict_of_pairs(self):
        text = (
            "OrderedDict([('interactor', {'possible_classes': "
            "[<class 'ethoscope.stimulators.composed_stimulator.ComposedStimulator'>], "
            "'class': <class 'ethoscope.stimulators.stimulators.DefaultStimulator'>, "
            "'kwargs': {}})])"
        )
        options = dbindex.parse_options(text)
        assert dbindex._option_name(options, "interactor") == ("DefaultStimulator", {})

    def test_old_firmware_and_unreadable(self):
        from conftest import STIM_OPTIONS

        assert dbindex._option_name(dbindex.parse_options(STIM_OPTIONS), "interactor")[0] == (
            "DefaultStimulator"
        )
        assert dbindex.parse_options("garbage{") is None
        assert dbindex.parse_options(None) is None
        assert dbindex.parse_options("[1, 2]") is None
