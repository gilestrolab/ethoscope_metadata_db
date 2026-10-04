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
    def test_invalid_dates_noted(self, tmp_path, census):
        from ethoscope_metadata_db.census import discover

        path = write_text(
            str(tmp_path / "us.csv"),
            "date,machine_name,region_id\n01/25/2024,7,1\n05/01/2024,7,2\n",
        )
        discover.ingest_files(census, [path])
        note = census.execute("SELECT note FROM meta_file").fetchone()[0]
        assert note == "1 date(s) not readable as dd/mm/yyyy or ISO"
        statuses = [r[0] for r in census.execute("SELECT date_status FROM meta_row ORDER BY id")]
        assert statuses == ["invalid", "ok"]

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
        assert info["date_status"] == {"ok": 5}
        assert {r["date"] for r in rows} == {"2024-01-10"}
        assert rows[1]["start_time"] == "15:00:00" and rows[2]["start_time"] is None
        assert rows[3]["start_time"] == "12:00:00"
