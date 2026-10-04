import os

from conftest import write_text
from ethoscope_metadata_db.census import discover


def _statuses(census):
    rows = census.execute("SELECT path, sheet, home, status, score FROM meta_file").fetchall()
    return {(os.path.basename(r["path"]), r["sheet"]): r for r in rows}


class TestWalk:
    def test_finds_files_and_homes_skipping_hidden(self, homes_root):
        found = {os.path.basename(p): home for p, home in discover.iter_tabular_files(homes_root)}
        assert found == {
            "metadata.csv": "alice",
            "titled.csv": "alice",
            "aliased.csv": "alice",
            "shopping.csv": "alice",
            "flies.xlsx": "bob",
            "timed.tsv": "carol",
        }

    def test_size_limit_and_extensions(self, homes_root):
        found = list(discover.iter_tabular_files(homes_root, {".tsv"}, max_bytes=10_000))
        assert [os.path.basename(p) for p, _ in found] == ["timed.tsv"]
        assert list(discover.iter_tabular_files(homes_root, {".tsv"}, max_bytes=10)) == []


class TestDiscover:
    def test_scores_and_statuses(self, homes_root, census):
        counts = discover.discover(census, homes_root)
        assert counts["seen"] == 6 and counts["registered"] == 6
        status = _statuses(census)
        assert status[("metadata.csv", "")]["status"] == "candidate"
        assert status[("metadata.csv", "")]["home"] == "alice"
        assert status[("metadata.csv", "")]["score"] == 1.0
        # ethoscopy would not read these: title row above the header, renamed columns.
        assert status[("titled.csv", "")]["status"] == "rejected"
        assert status[("aliased.csv", "")]["status"] == "rejected"
        assert status[("aliased.csv", "")]["score"] == 0.0
        assert status[("shopping.csv", "")]["status"] == "rejected"
        assert status[("flies.xlsx", "flies")]["status"] == "candidate"
        assert status[("flies.xlsx", "readme")]["status"] == "rejected"
        sha = census.execute("SELECT DISTINCT sha256 FROM meta_file WHERE path LIKE '%flies.xlsx'")
        assert len(sha.fetchall()) == 1

    def test_unreadable_file_recorded(self, tmp_path, census):
        bad = tmp_path / "h" / "dave" / "broken.xlsx"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"garbage")
        assert discover.discover(census, str(tmp_path / "h"))["unreadable"] == 1
        assert _statuses(census)[("broken.xlsx", "")]["status"] == "unreadable"

    def test_rescan_unchanged_touched_and_changed(self, homes_root, census):
        discover.discover(census, homes_root)
        discover.ingest_candidates(census)
        assert discover.discover(census, homes_root)["unchanged"] == 6

        path = os.path.join(homes_root, "carol", "timed.tsv")
        os.utime(path, (1, 1))
        assert discover.discover(census, homes_root)["touched"] == 1
        assert _statuses(census)[("timed.tsv", "")]["status"] == "ingested"

        with open(path, "a") as handle:
            handle.write("2024-01-05\tETHOSCOPE_007\t9\tCS\t\n")
        assert discover.discover(census, homes_root)["registered"] == 1
        assert _statuses(census)[("timed.tsv", "")]["status"] == "candidate"


class TestIngest:
    def test_ingest_candidates(self, homes_root, census):
        discover.discover(census, homes_root)
        counts = discover.ingest_candidates(census)
        assert counts == {"files": 3, "sheets": 3, "rows": 8 + 3 + 5, "failed": 0}
        row = census.execute("SELECT * FROM meta_file WHERE path LIKE '%metadata.csv'").fetchone()
        assert row["status"] == "ingested" and row["header_row"] == 0 and row["n_rows"] == 8
        assert row["note"] is None
        # Second run has nothing left to do.
        assert discover.ingest_candidates(census)["files"] == 0

    def test_ingest_named_file(self, tmp_path, census):
        path = write_text(str(tmp_path / "f.csv"), "date,machine_name,region_id\n2024-01-05,7,1\n")
        counts = discover.ingest_files(census, [path], home="manual")
        assert counts["rows"] == 1
        row = _statuses(census)[("f.csv", "")]
        assert row["status"] == "ingested" and row["home"] == "manual"

    def test_named_file_without_required_columns(self, tmp_path, census):
        path = write_text(str(tmp_path / "x.csv"), "date,machine,roi\n2024-01-05,7,1\n")
        discover.discover(census, str(tmp_path))
        assert _statuses(census)[("x.csv", "")]["status"] == "rejected"
        assert discover.ingest_files(census, [path])["rows"] == 0
        assert _statuses(census)[("x.csv", "")]["status"] == "no_key_columns"
        note = census.execute("SELECT note FROM meta_file").fetchone()[0]
        assert note == "missing column(s): machine_name, region_id"

    def test_file_vanished_before_ingest(self, tmp_path, census):
        path = write_text(
            str(tmp_path / "h" / "u" / "m.csv"), "date,machine_name,region_id\n2024-01-05,7,1\n"
        )
        discover.discover(census, str(tmp_path / "h"))
        os.remove(path)
        assert discover.ingest_candidates(census)["failed"] == 1
        assert _statuses(census)[("m.csv", "")]["status"] == "unreadable"

    def test_user_files_untouched(self, homes_root, census):
        files = [p for p, _ in discover.iter_tabular_files(homes_root)]
        before = {p: (os.stat(p).st_mtime, open(p, "rb").read()) for p in files}
        discover.discover(census, homes_root)
        discover.ingest_candidates(census)
        assert {p: (os.stat(p).st_mtime, open(p, "rb").read()) for p in files} == before
