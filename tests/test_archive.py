import csv
import datetime as dt
import os
import shutil

import pytest

from ethoscope_metadata_db.census import archive, cli, discover, store


@pytest.fixture
def ingested(homes_root, tmp_path):
    conn = store.connect(tmp_path / "census.sqlite")
    discover.discover(conn, homes_root)
    discover.ingest_candidates(conn)
    yield conn
    conn.close()


def accepted_paths(conn):
    return [
        r[0] for r in conn.execute("SELECT DISTINCT path FROM meta_file WHERE status = 'ingested'")
    ]


def catalogue(dest):
    with open(dest / "catalogue.csv", newline="") as fh:
        return list(csv.DictReader(fh))


class TestArchiveMetadata:
    def test_first_run_copies_every_accepted_file(self, ingested, tmp_path):
        dest = tmp_path / "archive"
        counts = archive.archive_metadata(ingested, dest, today=dt.date(2026, 10, 14))
        paths = accepted_paths(ingested)
        assert counts["files"] == len(paths) and counts["missing"] == 0
        assert counts["new_entries"] == len(paths)
        rows = catalogue(dest)
        assert {r["path"] for r in rows} == set(paths) and {r["first_seen"] for r in rows} == {
            "2026-10-14"
        }
        for row in rows:  # each stored copy is byte-identical to its source and named by its hash
            stored = archive.stored_path(dest, row["sha256"], row["path"])
            assert archive.file_sha256(stored) == row["sha256"] == archive.file_sha256(row["path"])

    def test_second_run_copies_nothing(self, ingested, tmp_path):
        dest = tmp_path / "archive"
        archive.archive_metadata(ingested, dest)
        again = archive.archive_metadata(ingested, dest)
        assert (again["copied"], again["new_entries"]) == (0, 0)

    def test_an_edited_sheet_keeps_both_versions(self, ingested, tmp_path):
        dest = tmp_path / "archive"
        archive.archive_metadata(ingested, dest)
        source = next(p for p in accepted_paths(ingested) if p.endswith(".csv"))
        before = archive.file_sha256(source)
        with open(source, "a") as fh:
            fh.write("\n")
        counts = archive.archive_metadata(ingested, dest)
        assert counts["copied"] == 1 and counts["new_entries"] == 1
        hashes = {r["sha256"] for r in catalogue(dest) if r["path"] == source}
        assert hashes == {before, archive.file_sha256(source)}
        assert all(archive.stored_path(dest, h, source).exists() for h in hashes)

    def test_identical_copies_are_stored_once(self, ingested, tmp_path, homes_root):
        source = next(p for p in accepted_paths(ingested) if p.endswith(".csv"))
        twin = os.path.join(homes_root, "twin_" + os.path.basename(source))
        shutil.copyfile(source, twin)
        ingested.execute(
            "INSERT INTO meta_file (path, sha256, status) VALUES (?, 'x', 'ingested')", (twin,)
        )
        counts = archive.archive_metadata(ingested, tmp_path / "archive")
        assert counts["contents"] == counts["files"] - 1  # the twin adds a row, not a file
        assert {r["path"] for r in catalogue(tmp_path / "archive")} >= {source, twin}

    def test_a_deleted_file_is_counted_missing(self, ingested, tmp_path):
        os.remove(accepted_paths(ingested)[0])
        counts = archive.archive_metadata(ingested, tmp_path / "archive")
        assert counts["missing"] == 1 and counts["new_entries"] == counts["files"] - 1

    def test_cli(self, ingested, tmp_path, capsys):
        census = ingested.execute("PRAGMA database_list").fetchone()[2]
        ingested.commit()
        assert cli.main(["--census", census, "archive-metadata", str(tmp_path / "a")]) == 0
        assert "archive-metadata: files=" in capsys.readouterr().out
