import pytest
from fastapi.testclient import TestClient

from ethoscope_metadata_db.census import dbindex, discover, match, store, summarise
from ethoscope_metadata_db.web.app import Settings, create_app

USER = {"X-Forwarded-Email": "tester@lab"}


@pytest.fixture
def census_path(results_root, homes_root, tmp_path):
    path = str(tmp_path / "census.sqlite")
    conn = store.connect(path)
    dbindex.scan_results(conn, results_root)
    discover.discover(conn, homes_root)
    discover.ingest_candidates(conn)
    match.match_all(conn)
    summarise.summarise(conn, tags=[(homes_root, "homes")])
    conn.close()
    return path


@pytest.fixture
def client(census_path):
    return TestClient(create_app(Settings(census_path)))


def _run_id(client, census_path, start):
    conn = store.connect(census_path)
    row = conn.execute("SELECT id FROM db_summary WHERE path_start = ?", (start,)).fetchone()
    conn.close()
    return row[0]


class TestAccess:
    def test_requires_the_sso_header(self, client):
        assert client.get("/").status_code == 401
        assert client.get("/", headers=USER).status_code == 200

    def test_health_and_static_are_open(self, client):
        health = client.get("/health").json()
        assert health["ok"] and health["summarised_at"]
        assert client.get("/static/style.css").status_code == 200

    def test_local_development_without_sso(self, census_path):
        open_client = TestClient(create_app(Settings(census_path, require_user=False)))
        assert open_client.get("/").status_code == 200

    def test_health_reports_a_missing_census(self, tmp_path):
        broken = TestClient(create_app(Settings(str(tmp_path / "none.sqlite"))))
        assert broken.get("/health").status_code == 503


class TestPages:
    def test_overview(self, client):
        page = client.get("/", headers=USER).text
        assert "Databases per month" in page and "<svg" in page and "tester@lab" in page
        assert "/runs?month=2024-01" in page  # every month links to its runs

    def test_runs_filter_sort_and_partial(self, client):
        page = client.get("/runs?category=single", headers=USER).text
        assert "<strong>1</strong> runs" in page and "2024-03-25 10:00:00" in page
        partial = client.get("/runs?category=orphan", headers={**USER, "HX-Request": "true"}).text
        assert "<html" not in partial and 'class="facets"' in partial
        by_size = client.get("/runs?sort=-size", headers=USER)
        assert by_size.status_code == 200

    def test_runs_text_search_and_none_facet(self, client):
        assert "<strong>3</strong> runs" in client.get("/runs?q=ETHOSCOPE_007", headers=USER).text
        none = client.get("/runs?stack=(none)", headers=USER).text
        assert "<strong>2</strong> runs" in none  # the two unreadable files have no kernel

    def test_run_page(self, client, census_path):
        run_id = _run_id(client, census_path, "2024-01-05T10:00:00")
        page = client.get(f"/runs/{run_id}", headers=USER).text
        assert "ETHOSCOPE_007" in page and "Tubes" in page and "metadata.csv" in page
        assert "Other runs on this machine that day" in page
        assert client.get("/runs/999999", headers=USER).status_code == 404

    def test_run_page_flags_genotype_conflicts(self, client, census_path):
        conn = store.connect(census_path)
        conn.execute(
            "UPDATE meta_row SET genotype_raw = 'w1118' WHERE region_id = 1 "
            "AND meta_file_id = (SELECT id FROM meta_file WHERE sheet = 'flies')"
        )
        conn.commit()
        conn.close()
        run_id = _run_id(client, census_path, "2024-01-05T10:00:00")
        assert "genotypes differ" in client.get(f"/runs/{run_id}", headers=USER).text

    def test_files_default_to_metadata(self, client):
        page = client.get("/files", headers=USER).text
        assert "<strong>3</strong> sheets" in page and "shopping.csv" not in page
        other = client.get("/files?outcome=not+metadata", headers=USER).text
        assert "shopping.csv" in other

    def test_file_page_and_ethoscopy_download(self, client, census_path):
        conn = store.connect(census_path)
        file_id = conn.execute(
            "SELECT id FROM file_summary WHERE path LIKE '%metadata.csv'"
        ).fetchone()[0]
        conn.close()
        page = client.get(f"/files/{file_id}", headers=USER).text
        assert "Rows not matched to one run" in page and "Runs it claims" in page
        csv = client.get(f"/files/{file_id}/ethoscopy.csv", headers=USER)
        assert csv.headers["content-type"].startswith("text/csv")
        lines = csv.text.strip().splitlines()
        assert lines[0] == "machine_name,date,time,region_id,genotype,sex,treatment,path"
        assert len(lines) == 1 + 3
        assert client.get("/files/999999", headers=USER).status_code == 404

    def test_search(self, client):
        page = client.get("/search?q=7", headers=USER).text
        assert "/runs?machine=ETHOSCOPE_007" in page
        page = client.get("/search?q=2024-01", headers=USER).text
        assert "/runs?month=2024-01" in page
        assert "/runs?day=2024-01-05" in client.get("/search?q=2024-01-05", headers=USER).text
        assert "w1118" in client.get("/search?q=w11", headers=USER).text
        assert "Nothing found" in client.get("/search?q=zzzz", headers=USER).text

    def test_exports_follow_the_selection(self, client):
        runs = client.get("/runs/export.csv?machine=ETHOSCOPE_007", headers=USER).text
        assert len(runs.strip().splitlines()) == 1 + 3
        tubes = client.get("/runs/ethoscopy.csv?category=multiple", headers=USER).text
        header, *rows = tubes.strip().splitlines()
        assert header.startswith("machine_name,date,time,region_id") and rows
        assert all(",ETHOSCOPE" not in r or "ETHOSCOPE_007" in r for r in rows)
