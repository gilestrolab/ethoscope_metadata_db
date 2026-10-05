import os

import pandas as pd
import pytest

from conftest import write_text
from ethoscope_metadata_db.census import cli, config, dbindex, discover, match, report, summarise


@pytest.fixture
def matched(results_root, homes_root, census):
    dbindex.scan_results(census, results_root)
    discover.discover(census, homes_root)
    discover.ingest_candidates(census)
    match.match_all(census)
    return census


def _table(conn, name):
    return pd.read_sql_query(f"SELECT * FROM {name}", conn)


class TestSummarise:
    def test_db_summary_agrees_with_db_claims(self, matched):
        summarise.summarise(matched)
        table = _table(matched, "db_summary").set_index("id")
        claims = report.db_claims(matched).set_index("id")
        assert (table["category"] == claims["category"]).all()
        row = table[table["path_start"] == "2024-01-05T10:00:00"].iloc[0]
        assert row["start_local"] == "2024-01-05T10:00:00" and row["month"] == "2024-01"
        assert row["experimenter"] == "alice" and row["n_sources"] == 2

    def test_file_summary_counts_and_tags(self, matched, homes_root):
        summarise.summarise(matched, tags=[(homes_root, "homes"), (homes_root + "/bob", "bob")])
        files = _table(matched, "file_summary").set_index("path")
        csv = files.loc[os.path.join(homes_root, "alice", "exp1", "metadata.csv")]
        assert csv["tag"] == "homes" and csv["n_rows"] == 8
        assert (csv["n_unique"], csv["n_roi_missing"], csv["n_not_found"]) == (3, 2, 2)
        assert csv["n_incomplete"] == 1 and csv["n_dbs"] == 2
        assert (csv["date_min"], csv["date_max"]) == ("2024-01-05", "2024-03-25")
        # The longest root wins.
        assert files.loc[os.path.join(homes_root, "bob", "flies.xlsx"), "tag"].iloc[0] == "bob"
        assert csv["outcome"] == "some unmatched" and csv["year"] == "2024"
        rejected = files.loc[os.path.join(homes_root, "alice", "notes", "shopping.csv")]
        assert rejected["status"] == "rejected" and rejected["n_copies"] == 0
        assert rejected["outcome"] == "not metadata"

    def test_rebuild_replaces_tables(self, matched):
        summarise.summarise(matched)
        counts = summarise.summarise(matched)
        assert counts == {
            "db_summary": 6,
            "file_summary": 7,
            "genotype_summary": 3,
            "claim_summary": 5,
        }
        indexes = {
            r[0] for r in matched.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"db_summary_key", "db_summary_category", "file_summary_tag"} <= indexes
        assert {"claim_summary_key", "claim_summary_file_id"} <= indexes
        assert matched.execute("SELECT value FROM schema_info WHERE key='summarised_at'").fetchone()

    def test_empty_census(self, census):
        assert summarise.summarise(census) == {
            "db_summary": 0,
            "file_summary": 0,
            "genotype_summary": 0,
            "claim_summary": 0,
        }

    def test_claim_summary_pairs(self, matched):
        summarise.summarise(matched)

        def pairs_of(sql, params=()):
            return {tuple(r) for r in matched.execute(sql, params)}

        pairs = pairs_of("SELECT db_id, file_id FROM claim_summary")
        expected = pairs_of(
            "SELECT m.db_file_id, r.meta_file_id FROM match m "
            "JOIN meta_row r ON r.id = m.meta_row_id "
            f"WHERE m.status IN ({', '.join('?' * len(report.CLAIM_STATUSES))})",
            list(report.CLAIM_STATUSES),
        )
        assert pairs == expected and len(pairs) == 5
        # Passed-over runs (an earlier run of the day, another copy) are not claims.
        passed_over = pairs_of(
            "SELECT m.db_file_id, r.meta_file_id FROM match m "
            "JOIN meta_row r ON r.id = m.meta_row_id WHERE m.status IN ('superseded', 'duplicate')"
        )
        assert passed_over and not pairs & passed_over

    def test_tag_for(self):
        tags = [("/a", "a"), ("/a/b/", "b")]
        assert summarise.tag_for("/a/x.csv", tags) == "a"
        assert summarise.tag_for("/a/b/x.csv", tags) == "b"
        assert summarise.tag_for("/ab/x.csv", tags) is None


TOML = """
timezone = "Europe/London"

[[results]]
path = "{results}"

[[metadata]]
path = "{homes}"
tag = "homes"
exclude = ["bob"]

[[metadata]]
path = "{homes}/bob"
tag = "bob"
files_from = "bob_list.txt"
"""


class TestConfigAndRefresh:
    def test_load_config(self, tmp_path):
        path = write_text(str(tmp_path / "census.toml"), TOML.format(results="/r", homes="/h"))
        loaded = config.load_config(path)
        assert [r.path for r in loaded.results] == ["/r"]
        assert loaded.metadata[0].exclude == ("bob",) and loaded.metadata[0].txt
        assert loaded.metadata[1].files_from == str(tmp_path / "bob_list.txt")
        assert loaded.tags == [("/h", "homes"), ("/h/bob", "bob")]

    @pytest.mark.parametrize(
        "text",
        ["oops = 1\n", "[[results]]\nfiles_from = 'x'\n", "[[metadata]]\npath = '/h'\n"],
    )
    def test_bad_config(self, tmp_path, text):
        path = write_text(str(tmp_path / "bad.toml"), text)
        with pytest.raises(config.ConfigError):
            config.load_config(path)

    def test_refresh_end_to_end(self, results_root, homes_root, tmp_path, capsys):
        write_text(str(tmp_path / "bob_list.txt"), "flies.xlsx\n")
        path = write_text(
            str(tmp_path / "census.toml"), TOML.format(results=results_root, homes=homes_root)
        )
        census = str(tmp_path / "c.sqlite")
        assert cli.main(["--census", census, "refresh", "--config", path]) == 0
        out = capsys.readouterr().out
        assert "match: " in out and "summarise: {'db_summary': 6" in out
        assert "discover bob" in out
        conn = cli.store.connect(census)
        tags = dict(conn.execute("SELECT path, tag FROM file_summary").fetchall())
        assert tags[os.path.join(homes_root, "bob", "flies.xlsx")] == "bob"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        conn.close()

    def test_refresh_reports_missing_config(self, tmp_path, capsys):
        census = str(tmp_path / "c.sqlite")
        assert cli.main(["--census", census, "refresh", "--config", "/nope.toml"]) == 2
        assert "refresh failed" in capsys.readouterr().err
