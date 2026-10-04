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
    assert _category(claims, "2024-04-01T10:00:00") == "orphan"
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
    assert tables["dbs_by_stack"].loc["unknown", "orphan"] == 2
    assert tables["rows_by_home"].loc["alice"].sum() == 8
    assert "superseded" not in tables["rows_by_home"].columns
    assert tables["dbs_by_machine_name"].loc["ETHOSCOPE_007", "superseded"] == 1


def test_text_summary_and_write(matched, tmp_path):
    text = report.text_summary(matched)
    assert "databases indexed: 6 (2 unreadable)" in text and "orphan" in text
    written = report.write_report(matched, str(tmp_path / "out"))
    names = {os.path.basename(p) for p in written}
    assert {"db_claims.csv", "orphans.csv", "unmatched_rows.csv", "annotated_tubes.csv"} <= names
    assert len(pd.read_csv(tmp_path / "out" / "orphans.csv")) == 2


def test_empty_census(census):
    assert report.db_claims(census).empty
    assert report.unmatched_rows(census).empty
    assert report.annotated_tubes(census).empty
    assert "databases indexed: 0" in report.text_summary(census)
