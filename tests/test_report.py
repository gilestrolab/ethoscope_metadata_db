import os

import pandas as pd
import pytest

from ethoscope_metadata_db.census import dbindex, discover, match, report


@pytest.fixture
def matched(results_root, homes_root, census):
    dbindex.scan_results(census, results_root)
    discover.discover(census, homes_root)
    discover.ingest_candidates(census)
    match.match_all(census)
    return census


def _category(claims, start):
    return claims.set_index("path_start").loc[start, "category"]


def test_db_claims_categories(matched):
    claims = report.db_claims(matched)
    assert len(claims) == 6
    # alice (csv) and bob (xlsx) both claim the 5 January run.
    assert _category(claims, "2024-01-05T10:00:00") == "multiple"
    assert _category(claims, "2024-03-25T10:00:00") == "single"
    # bob and carol both claim the last run of 10 January; the 09:00 false
    # start is only ever passed over.
    assert _category(claims, "2024-01-10T15:00:00") == "multiple"
    assert _category(claims, "2024-01-10T09:00:00") == "superseded"
    # The synthetic files are a few KB: unclaimed ones are misfires...
    assert _category(claims, "2024-04-01T10:00:00") == "misfire"
    assert _category(claims, "2024-05-01T10:00:00") == "misfire"
    # ...and orphans when no size or length marks them as failed starts.
    claims = report.db_claims(matched, misfire_mb=0, misfire_h=0)
    assert _category(claims, "2024-05-01T10:00:00") == "orphan"
    assert set(claims["year"]) == {"2024"}


def test_multiply_claimed_genotype_agreement(matched):
    multi = report.multiply_claimed(matched)
    # Region 1 of the 5 January run is in both alice's csv and bob's xlsx.
    assert list(multi["region_id"]) == [1]
    assert multi["genotype_agrees"].all() and set(multi["genotypes"]) == {"CS"}


def test_multiply_claimed_disagreement(matched):
    matched.execute(
        "UPDATE meta_row SET genotype_raw = 'w1118' WHERE region_id = 1 AND genotype_raw = 'CS' "
        "AND meta_file_id = (SELECT id FROM meta_file WHERE sheet = 'flies')"
    )
    multi = report.multiply_claimed(matched)
    assert not multi.loc[0, "genotype_agrees"]
    assert multi.loc[0, "genotypes"] == "CS|w1118"


def test_unmatched_rows_lists_candidates(matched):
    rows = report.unmatched_rows(matched)
    assert len(rows) == 6  # 16 rows minus 10 unique
    assert "superseded" not in set(rows["status"])
    timed = rows[rows["status"] == "not_found"].set_index("row_idx")
    assert "runs that day" in timed.loc[4, "note"]


def test_annotated_tubes_and_query(matched):
    tubes = report.annotated_tubes(matched)
    assert len(tubes) == 10
    assert {"path", "machine_name", "date", "time", "region_id", "genotype", "stack"} <= set(
        tubes.columns
    )
    assert set(tubes.loc[tubes["date"] == "2024-01-10", "time"]) == {"15-00-00"}
    stim = report.query(
        matched,
        "genotype in ['CS', 'w1118'] and interactor == 'DefaultStimulator' and stack == 'picamera2'",
    )
    assert len(stim) == 4 and stim["path"].nunique() == 1
    assert report.query(matched, "genotype == 'nonexistent'").empty
    with pytest.raises(Exception):
        report.query(matched, "no_such_column == 1")


def test_summaries(matched):
    tables = report.summaries(matched)
    assert tables["dbs_by_stack"].loc["legacy", "total"] == 1
    assert tables["dbs_by_stack"].loc["unknown", "misfire"] == 2
    assert tables["rows_by_home"].loc["alice", "total"] == 8
    assert "superseded" not in tables["rows_by_home"].columns
    assert tables["dbs_by_machine_name"].loc["ETHOSCOPE_007", "superseded"] == 1
    # Rows without a usable date count under "unknown" year.
    assert tables["rows_by_year"]["total"].sum() == 16
    assert tables["rows_by_year"].loc["unknown", "incomplete"] == 1
    assert tables["rows_by_machine"].loc["ETHOSCOPE_099", "not_found"] == 1
    # The 5 January run is claimed from alice and bob: counted under both homes.
    assert tables["dbs_by_home"].loc["alice", "multiple"] == 1
    assert tables["dbs_by_home"].loc["bob", "multiple"] == 2


def test_header_counts(matched):
    headers = report.header_counts(matched).set_index("header")
    # Exact ethoscopy names in alice, bob and carol; near misses only in rejected sheets.
    assert headers.loc["machine_name", "n_homes"] == 3
    assert headers.loc["machine_name", "n_accepted"] == 3
    assert headers.loc["Ethoscope", "n_accepted"] == 0 and headers.loc["ROI", "n_files"] == 1
    assert headers.loc["About", "n_sheets"] == 1


def test_date_shapes(matched):
    shapes = report.date_shapes(matched).set_index(["shape", "date_status"])["n_rows"]
    assert shapes[("99/99/9999", "ok")] == 7
    assert shapes[("99/99/9999", "ambiguous")] == 1  # carol's 10/01/2024
    assert shapes[("9999-99-99", "ok")] == 4
    assert shapes[("9999-99-99a99:99:99", "ok")] == 3
    assert shapes[("(blank)", "missing")] == 1
    assert shapes.sum() == 16


def test_text_summary_and_write(matched, tmp_path):
    text = report.text_summary(matched)
    assert "databases indexed: 6 (2 unreadable)" in text and "misfire" in text
    written = report.write_report(matched, str(tmp_path / "out"))
    names = {os.path.basename(p) for p in written}
    assert {"db_claims.csv", "orphans.csv", "unmatched_rows.csv", "annotated_tubes.csv"} <= names
    assert {"header_counts.csv", "date_shapes.csv", "rows_by_year.csv"} <= names
    claims = pd.read_csv(tmp_path / "out" / "db_claims.csv")
    assert (claims["category"] == "misfire").sum() == 2
    assert pd.read_csv(tmp_path / "out" / "orphans.csv").empty


def test_empty_census(census):
    assert report.db_claims(census).empty
    assert report.unmatched_rows(census).empty
    assert report.annotated_tubes(census).empty
    assert report.header_counts(census).empty and report.date_shapes(census).empty
    assert "databases indexed: 0" in report.text_summary(census)


def test_copies_and_empty_files(results_root, homes_root, census):
    import datetime as dt
    import shutil

    from conftest import tree_db_path

    original = tree_db_path(results_root, 7, dt.datetime(2024, 1, 5, 10))
    shutil.copy(original, original.replace(".db", "_backup_20250716_135339.db"))
    late_start = tree_db_path(results_root, 12, dt.datetime(2024, 3, 25, 18))
    stray = tree_db_path(results_root, 12, dt.datetime(2024, 6, 1, 9))
    for path in (late_start, stray):
        os.makedirs(os.path.dirname(path))
        open(path, "wb").close()
    dbindex.scan_results(census, results_root)
    discover.discover(census, homes_root)
    discover.ingest_candidates(census)
    match.match_all(census)
    claims = report.db_claims(census).set_index("path")
    assert claims.loc[original, "category"] == "multiple"
    assert (
        claims.loc[original.replace(".db", "_backup_20250716_135339.db"), "category"] == "duplicate"
    )
    # An empty later start never replaces the day's real run.
    assert claims.loc[late_start, "category"] == "superseded"
    assert _category(claims.reset_index(), "2024-03-25T10:00:00") == "single"
    assert claims.loc[stray, "category"] == "empty"


def test_date_shapes_with_datetime_and_split_columns(tmp_path, census):
    from conftest import write_text

    write_text(
        str(tmp_path / "h" / "u" / "a.txt"),
        "machine_name\tdatetime\tregion_id\nETHOSCOPE_007\t1704877200\t1\n",
    )
    write_text(
        str(tmp_path / "h" / "u" / "b.csv"),
        "machine_name,region_id,day,month,year\nETHOSCOPE_007,1,5,1,2024\n",
    )
    discover.discover(census, str(tmp_path / "h"))
    discover.ingest_candidates(census)
    shapes = report.date_shapes(census).set_index(["shape", "date_status"])["n_rows"]
    assert shapes[("9999999999", "ok")] == 1 and shapes[("(blank)", "ok")] == 1
