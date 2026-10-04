import sqlite3

import pytest

from ethoscope_metadata_db.census import store


def test_connect_creates_schema(tmp_path):
    conn = store.connect(tmp_path / "c.sqlite")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"db_file", "meta_file", "meta_row", "match", "vocab", "schema_info"} <= tables
    conn.close()
    # Re-opening an existing census is fine.
    store.connect(tmp_path / "c.sqlite").close()


def test_upsert_keeps_id(census):
    first = store.upsert_db_file(census, {"path": "/a.db", "size": 1, "readable": 1})
    second = store.upsert_db_file(census, {"path": "/a.db", "size": 2, "readable": 1})
    assert first == second
    assert store.get_db_file(census, "/a.db")["size"] == 2
    assert store.get_db_file(census, "/missing.db") is None


def test_deleting_meta_file_cascades(census):
    db_id = store.upsert_db_file(census, {"path": "/a.db", "readable": 1})
    mf = store.insert_meta_file(census, {"path": "/m.csv", "sheet": "", "status": "candidate"})
    store.replace_meta_rows(census, mf, [{"row_idx": 1, "raw_json": "{}"}])
    row_id = census.execute("SELECT id FROM meta_row").fetchone()[0]
    census.execute("INSERT INTO match VALUES (?, ?, 'unique', NULL, NULL)", (row_id, db_id))
    store.delete_meta_path(census, "/m.csv")
    assert census.execute("SELECT count(*) FROM meta_row").fetchone()[0] == 0
    assert census.execute("SELECT count(*) FROM match").fetchone()[0] == 0


def test_replace_meta_rows_replaces(census):
    mf = store.insert_meta_file(census, {"path": "/m.csv", "sheet": "", "status": "candidate"})
    store.replace_meta_rows(census, mf, [{"row_idx": i, "raw_json": "{}"} for i in range(3)])
    assert store.replace_meta_rows(census, mf, [{"row_idx": 9, "raw_json": "{}"}]) == 1
    assert census.execute("SELECT row_idx FROM meta_row").fetchall()[0][0] == 9


def test_meta_file_path_sheet_unique(census):
    store.insert_meta_file(census, {"path": "/m.xlsx", "sheet": "a", "status": "candidate"})
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_meta_file(census, {"path": "/m.xlsx", "sheet": "a", "status": "candidate"})


def test_wrong_schema_version_refused(tmp_path):
    path = tmp_path / "c.sqlite"
    conn = store.connect(path)
    conn.execute("UPDATE schema_info SET value = '999' WHERE key = 'version'")
    conn.commit()
    conn.close()
    with pytest.raises(store.SchemaVersionError):
        store.connect(path)
