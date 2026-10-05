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


def test_schema_1_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO schema_info VALUES ('version', '1');
        CREATE TABLE meta_file (id INTEGER PRIMARY KEY, path TEXT NOT NULL,
                                sheet TEXT NOT NULL DEFAULT '', status TEXT NOT NULL);
        CREATE TABLE meta_row (id INTEGER PRIMARY KEY, meta_file_id INTEGER NOT NULL,
                               row_idx INTEGER NOT NULL, raw_json TEXT NOT NULL);
        CREATE TABLE db_file (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, machine_name TEXT,
                              readable INTEGER NOT NULL DEFAULT 0);
        INSERT INTO meta_file VALUES (1, '/m.csv', '', 'ingested');
        """)
    raw.close()
    conn = store.connect(path)
    assert "date_alt" in {r[1] for r in conn.execute("PRAGMA table_info(meta_row)")}
    assert "experimenter" in {r[1] for r in conn.execute("PRAGMA table_info(db_file)")}
    row = conn.execute("SELECT status, date_order FROM meta_file").fetchone()
    assert row["status"] == "ingested" and row["date_order"] is None
    assert conn.execute("SELECT value FROM schema_info").fetchone()[0] == store.SCHEMA_VERSION
    conn.close()
    store.connect(path).close()  # already current: nothing to migrate
