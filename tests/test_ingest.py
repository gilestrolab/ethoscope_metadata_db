import datetime as dt
import json
import os

import pandas as pd
import pytest

from conftest import write_text
from ethoscope_metadata_db.census import ingest


class TestColumns:
    def test_ethoscopy_columns_exact(self):
        headers = ["date", "machine_name", "region_id", "time", "Genotype ", "SEX", "notes"]
        mapping, notes = ingest.map_columns(headers)
        assert mapping == {
            "date": 0,
            "machine_name": 1,
            "region_id": 2,
            "time": 3,
            "genotype": 4,
            "sex": 5,
        }
        assert notes == [] and ingest.is_metadata(mapping)

    @pytest.mark.parametrize(
        "headers",
        [
            ["Date", "machine_name", "region_id"],
            ["date ", "machine_name", "region_id"],
            ["date", "ethoscope", "roi"],
            ["date", "machine_name"],
        ],
    )
    def test_near_misses_are_not_metadata(self, headers):
        assert not ingest.is_metadata(ingest.map_columns(headers)[0])

    def test_repeated_column(self):
        mapping, notes = ingest.map_columns(["date", "date", "machine_name", "region_id"])
        assert mapping["date"] == 0 and notes == ["repeated date column ignored"]

    def test_score(self):
        assert ingest.score_columns({"date": 0, "machine_name": 1, "region_id": 2}) == 1.0
        assert ingest.score_columns({"date": 0, "genotype": 1}) == 0.333
        assert ingest.score_columns({}) == 0.0

    def test_header_is_first_row(self):
        titled = pd.DataFrame(
            [["My experiment", None, None], ["date", "machine_name", "region_id"]]
        )
        row, mapping, _ = ingest.find_header(titled)
        assert row == 0 and not ingest.is_metadata(mapping)

    def test_find_header_empty(self):
        assert ingest.find_header(pd.DataFrame())[0] is None


class TestReadTabular:
    def test_semicolon_cp1252_with_ragged_rows(self, homes_root):
        path = os.path.join(homes_root, "alice", "exp1", "metadata.csv")
        [(sheet, frame)] = ingest.read_tabular(path)
        assert sheet == "" and frame.shape == (10, 6)
        assert "–" in frame.iloc[2, 5]

    def test_xlsx_all_sheets_keep_types(self, homes_root):
        sheets = dict(ingest.read_tabular(os.path.join(homes_root, "bob", "flies.xlsx")))
        assert set(sheets) == {"flies", "readme"}
        assert isinstance(sheets["flies"].iloc[1, 2], dt.datetime)

    def test_xls(self, tmp_path):
        xlwt = pytest.importorskip("xlwt")
        book = xlwt.Workbook()
        sheet = book.add_sheet("s1")
        for col, value in enumerate(["date", "machine_name", "region_id"]):
            sheet.write(0, col, value)
        for col, value in enumerate([45296, "ETHOSCOPE_007", 1]):
            sheet.write(1, col, value)
        path = str(tmp_path / "old.xls")
        book.save(path)
        [(name, frame)] = ingest.read_tabular(path)
        assert name == "s1" and frame.shape == (2, 3)

    def test_unsupported_and_corrupt(self, tmp_path):
        with pytest.raises(ValueError):
            ingest.read_tabular(str(tmp_path / "a.pdf"))
        bad = tmp_path / "bad.xlsx"
        bad.write_bytes(b"not a zip")
        with pytest.raises(Exception):
            ingest.read_tabular(str(bad))

    def test_empty_csv(self, tmp_path):
        path = write_text(str(tmp_path / "e.csv"), "")
        [(_, frame)] = ingest.read_tabular(path)
        assert frame.empty


class TestBuildRows:
    def test_month_first_sheet_is_read_month_first(self, tmp_path, census):
        from ethoscope_metadata_db.census import discover

        path = write_text(
            str(tmp_path / "us.csv"),
            "date,machine_name,region_id\n01/25/2024,7,1\n05/01/2024,7,2\n13/13/2024,7,3\n",
        )
        discover.ingest_files(census, [path])
        sheet = census.execute("SELECT note, date_order FROM meta_file").fetchone()
        assert sheet["date_order"] == "mdy" and sheet["note"] == "1 date(s) not readable"
        rows = census.execute("SELECT date, date_status FROM meta_row ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [
            ("2024-01-25", "ok"),
            ("2024-05-01", "ok"),
            (None, "invalid"),
        ]

    def test_unknown_order_keeps_both_readings(self, tmp_path):
        path = write_text(
            str(tmp_path / "u.csv"), "date,machine_name,region_id\n05/01/2024,7,1\n03/03/2024,7,2\n"
        )
        rows, info = self._rows(path)
        assert info["date_order"] == "unknown"
        assert [(r["date"], r["date_status"], r["date_alt"]) for r in rows] == [
            ("2024-01-05", "ambiguous", "2024-05-01"),
            ("2024-03-03", "ok", None),
        ]

    def test_day_month_year_columns(self, tmp_path):
        path = write_text(
            str(tmp_path / "split.csv"),
            "machine_name,region_id,day,month,year,date\n"
            "7,1,5,1,2024,05/01/2024\n"
            "7,2,5,1,2024,2024-02-05\n"
            "7,3,,,,\n",
        )
        rows, info = self._rows(path)
        assert info["date_order"] == "split" and info["date_disagree"] == 1
        assert [(r["date"], r["date_status"], r["date_alt"]) for r in rows] == [
            ("2024-01-05", "ok", None),
            ("2024-01-05", "ok", None),
            (None, "missing", None),
        ]

    def test_day_month_year_satisfy_the_date_column(self):
        mapping, _ = ingest.map_columns(["machine_name", "region_id", "day", "month", "year"])
        assert ingest.is_metadata(mapping) and ingest.score_columns(mapping) == 1.0
        mapping, _ = ingest.map_columns(["machine_name", "region_id", "day", "month"])
        assert ingest.missing_columns(mapping) == ["date"]
        assert ingest.score_columns(mapping) == 0.667

    def _rows(self, path):
        [(_, frame)] = ingest.read_tabular(path)
        header, mapping, _ = ingest.find_header(frame)
        return ingest.build_rows(frame, header, mapping)

    def test_messy_csv_rows(self, homes_root):
        rows, info = self._rows(os.path.join(homes_root, "alice", "exp1", "metadata.csv"))
        assert len(rows) == 8  # the blank line is dropped
        first = rows[0]
        assert first["row_idx"] == 1
        assert (first["date"], first["date_status"]) == ("2024-01-05", "ok")
        assert first["machine_name"] == "ETHOSCOPE_007" and first["region_id"] == 1
        assert rows[1]["genotype_raw"] == "CS" and rows[1]["sex"] == "M"
        assert json.loads(rows[1]["raw_json"])["Genotype"] == " CS "
        assert json.loads(rows[1]["other_json"]) == {"Notes": "dead day 2 – é"}
        assert rows[3]["date_status"] == "ok" and rows[3]["date"] == "2024-03-25"
        assert rows[-1]["date_status"] == "missing" and rows[-1]["machine_name"] == "ETHOSCOPE_007"

    def test_dates_and_times(self, homes_root):
        rows, info = self._rows(os.path.join(homes_root, "carol", "timed.tsv"))
        # 10/01/2024 is the sheet's only day/month string: 10 January or 1 October.
        assert info["date_order"] == "unknown"
        assert info["date_status"] == {"ambiguous": 1, "ok": 4}
        assert (rows[0]["date"], rows[0]["date_alt"]) == ("2024-01-10", "2024-10-01")
        assert {r["date"] for r in rows} == {"2024-01-10"}
        assert rows[1]["start_time"] == "15:00:00" and rows[2]["start_time"] is None
        assert rows[3]["start_time"] == "12:00:00"

    def test_stamp_as_date_picks_the_run(self, tmp_path):
        path = write_text(
            str(tmp_path / "stamped.csv"),
            "date,machine_name,region_id,time\n"
            "2024-01-10_09-00-00,7,1,\n"
            "2024-01-10_09-00-00,7,2,15:00\n"
            "2024-01-10,7,3,\n",
        )
        rows, info = self._rows(path)
        assert info["date_status"] == {"ok": 3}
        assert [r["start_time"] for r in rows] == ["09:00:00", "15:00:00", None]

    def test_rethomics_datetime(self, tmp_path):
        path = write_text(
            str(tmp_path / "linked.txt"),
            "id\tmachine_name\tmachine_id\tdatetime\tregion_id\ttime\n"
            "a\tETHOSCOPE_007\t007cc\t2024-01-10 09:00:00\t1\t\n"
            "b\tETHOSCOPE_007\t007cc\t2024-01-10 09:00:00\t2\t15:00\n",
        )
        rows, info = self._rows(path)
        assert [(r["date"], r["start_time"]) for r in rows] == [
            ("2024-01-10", "09:00:00"),
            ("2024-01-10", "15:00:00"),  # an explicit time column wins
        ]

    def test_datetime_satisfies_the_date_column(self):
        mapping, _ = ingest.map_columns(["machine_name", "datetime", "region_id"])
        assert ingest.is_metadata(mapping)
        mapping, _ = ingest.map_columns(["machine_name", "start_datetime", "region_id"])
        assert ingest.missing_columns(mapping) == ["date"]

    def test_rethomics_epoch_datetime(self, tmp_path):
        # R writes POSIXct as seconds; 1704877200 is 2024-01-10 09:00:00 UTC.
        path = write_text(
            str(tmp_path / "posix.txt"),
            "machine_name\tdatetime\tregion_id\nETHOSCOPE_007\t1704877200\t1\n",
        )
        rows, _ = self._rows(path)
        assert (rows[0]["date"], rows[0]["start_time"]) == ("2024-01-10", "09:00:00")


class TestRRowNames:
    def test_write_table_row_names_are_aligned(self, tmp_path):
        # write.table(row.names = TRUE): no header cell for the row names.
        path = write_text(
            str(tmp_path / "r.txt"),
            "machine_name\tdate\tregion_id\n"
            "1\tETHOSCOPE_007\t2024-01-05\t3\n"
            "2\tETHOSCOPE_007\t2024-01-05\t4\n",
        )
        [(_, frame)] = ingest.read_tabular(path)
        header, mapping, _ = ingest.find_header(frame)
        rows, info = ingest.build_rows(frame, header, mapping)
        assert info["columns"][0] == "column_1"
        assert [(r["machine_name"], r["region_id"]) for r in rows] == [
            ("ETHOSCOPE_007", 3),
            ("ETHOSCOPE_007", 4),
        ]

    def test_trailing_delimiter_is_not_a_row_name(self):
        rows = [["a", "b"], ["1", "2", ""], ["3", "4", ""]]
        assert ingest._align_r_row_names(rows) == rows
