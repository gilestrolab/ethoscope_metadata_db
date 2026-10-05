import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from ethoscope_metadata_db.census import dbindex, discover, match, store as census_store, summarise
from ethoscope_metadata_db.web.app import Settings, create_app
from ethoscope_metadata_db.web.curation import actions, membership, store, suggest, views
from ethoscope_metadata_db.web.curation.store import CurationError

WHO = "tester@lab"


@pytest.fixture
def cur(tmp_path):
    conn = store.connect(tmp_path / "curation.sqlite")
    yield conn
    conn.close()


@pytest.fixture
def census_path(results_root, homes_root, tmp_path):
    path = str(tmp_path / "census.sqlite")
    conn = census_store.connect(path)
    dbindex.scan_results(conn, results_root)
    discover.discover(conn, homes_root)
    discover.ingest_candidates(conn)
    match.match_all(conn)
    summarise.summarise(conn, tags=[(homes_root, "homes")])
    conn.close()
    return path


@pytest.fixture
def both(census_path, tmp_path):
    """The census with a curation file attached as ``cur`` (as the web app does)."""
    cur_path = str(tmp_path / "curation.sqlite")
    store.connect(cur_path).close()
    conn = census_store.connect(census_path)
    conn.execute("ATTACH DATABASE ? AS cur", (cur_path,))
    writable = store.connect(cur_path)
    yield conn, writable
    conn.close()
    writable.close()


class TestChangesAndUndo:
    def test_create_label_and_undo(self, cur):
        label_id = actions.create_label(cur, WHO, "  Sleep   rebound ", "project", doi="10.1/x")
        row = cur.execute("SELECT * FROM label").fetchone()
        assert (row["id"], row["name"], row["kind"], row["doi"]) == (
            label_id,
            "Sleep rebound",
            "project",
            "10.1/x",
        )
        audit = cur.execute("SELECT * FROM audit").fetchone()
        assert audit["who"] == WHO and audit["action"] == "create label"
        store.undo(cur, audit["id"], WHO)
        assert cur.execute("SELECT count(*) FROM label").fetchone()[0] == 0
        with pytest.raises(CurationError):
            store.undo(cur, audit["id"], WHO)

    def test_duplicate_and_bad_names(self, cur):
        actions.create_label(cur, WHO, "CS")
        with pytest.raises(CurationError):
            actions.create_label(cur, WHO, "cs")  # names are case-insensitive
        with pytest.raises(CurationError):
            actions.create_label(cur, WHO, "   ")
        with pytest.raises(CurationError):
            actions.create_label(cur, WHO, "x", kind="weird")

    def test_delete_label_undo_restores_its_links(self, cur):
        label_id = actions.create_label(cur, WHO, "A")
        assert actions.link(cur, WHO, label_id, "run", ["/r/1.db", "/r/2.db", "/r/1.db"]) == 2
        assert actions.link(cur, WHO, label_id, "run", ["/r/1.db"]) == 0  # already there
        actions.delete_label(cur, WHO, label_id)
        assert cur.execute("SELECT count(*) FROM label_link").fetchone()[0] == 0
        last = cur.execute("SELECT max(id) FROM audit").fetchone()[0]
        store.undo(cur, last, WHO)
        assert (
            cur.execute(
                "SELECT count(*) FROM label_link WHERE label_id = ?", (label_id,)
            ).fetchone()[0]
            == 2
        )

    def test_merge_labels(self, cur):
        a, b = actions.create_label(cur, WHO, "a"), actions.create_label(cur, WHO, "b")
        actions.link(cur, WHO, a, "file", ["/f1", "/f2"])
        actions.link(cur, WHO, b, "file", ["/f2"])
        actions.merge_labels(cur, WHO, a, b)
        targets = {
            r[0] for r in cur.execute("SELECT target FROM label_link WHERE label_id = ?", (b,))
        }
        assert targets == {"/f1", "/f2"}
        assert cur.execute("SELECT count(*) FROM label WHERE id = ?", (a,)).fetchone()[0] == 0
        with pytest.raises(CurationError):
            actions.merge_labels(cur, WHO, b, b)

    def test_people_aliases_move_and_merge(self, cur):
        p1 = actions.create_person(
            cur, WHO, "Grace Hopper", [("experimenter", "grace.hopper"), ("home", "ghopper")]
        )
        p2 = actions.create_person(cur, WHO, "G. Hopper", [("home", "ghopper")])  # takes the alias over
        owner = cur.execute("SELECT person_id FROM person_alias WHERE value = 'ghopper'").fetchone()[0]
        assert owner == p2
        actions.merge_people(cur, WHO, p2, p1)
        aliases = {
            r[0] for r in cur.execute("SELECT value FROM person_alias WHERE person_id = ?", (p1,))
        }
        assert aliases == {"grace.hopper", "ghopper"}
        last = cur.execute("SELECT max(id) FROM audit").fetchone()[0]
        store.undo(cur, last, WHO)  # un-merge
        assert (
            cur.execute("SELECT person_id FROM person_alias WHERE value = 'ghopper'").fetchone()[0]
            == p2
        )

    def test_notes(self, cur):
        actions.add_note(cur, WHO, "run", "/r/1.db", "  lights failed on day 2 ")
        note = cur.execute("SELECT * FROM note").fetchone()
        assert note["text"] == "lights failed on day 2" and note["who"] == WHO
        with pytest.raises(CurationError):
            actions.add_note(cur, WHO, "run", "/r/1.db", " ")
        actions.delete_note(cur, WHO, note["id"])
        assert cur.execute("SELECT count(*) FROM note").fetchone()[0] == 0


class TestMembership:
    def test_labels_via_file_folder_and_run(self, both, homes_root, results_root):
        census, cur = both
        csv = os.path.join(homes_root, "alice", "exp1", "metadata.csv")
        label = actions.create_label(cur, WHO, "alice exp1")
        actions.link(cur, WHO, label, "file", [csv])
        folder_label = actions.create_label(cur, WHO, "bob's")
        actions.link(cur, WHO, folder_label, "folder", [os.path.join(homes_root, "bob") + "/"])
        counts = membership.rebuild(census, cur)
        assert counts["file_label"] == 1 + 2  # the csv; both sheets of bob's xlsx
        via = dict(
            census.execute(
                "SELECT via, count(*) FROM cur.run_label WHERE label_id = ? GROUP BY 1", (label,)
            ).fetchall()
        )
        assert via == {"metadata": 2}  # the two runs the csv claims
        assert (
            census.execute(
                "SELECT count(*) FROM cur.run_label WHERE label_id = ?", (folder_label,)
            ).fetchone()[0]
            == 2
        )

    def test_people_via_aliases_and_freshness(self, both):
        census, cur = both
        actions.create_person(cur, WHO, "Alice", [("experimenter", "alice"), ("home", "alice")])
        assert membership.ensure_fresh(census, cur) is True
        assert membership.ensure_fresh(census, cur) is False  # nothing changed
        assert census.execute("SELECT count(*) FROM cur.run_person").fetchone()[0] == 4
        # alice's one metadata sheet; her other spreadsheets are not metadata and have no owner
        assert census.execute("SELECT count(*) FROM cur.file_person").fetchone()[0] == 1


class TestSuggestions:
    def test_alias_groups(self, both):
        census, cur = both
        census.execute(
            "UPDATE db_summary SET experimenter = 'Alice' WHERE machine_name = 'ETHOSCOPE_012'"
        )
        census.execute(
            "UPDATE db_summary SET experimenter = 'carol.jones' WHERE machine_name = 'ETHOSCOPE_020'"
        )
        census.execute("UPDATE file_summary SET home = 'cjones' WHERE home = 'carol'")
        census.commit()  # an open transaction would keep reading an old snapshot of ``cur``

        def groups():
            return [
                {(m["kind"], m["value"]) for m in g["members"]}
                for g in suggest.alias_groups(census)
            ]

        found = groups()
        # Case variants of one name, and the experimenter home of the same name, form one group.
        assert {("experimenter", "alice"), ("experimenter", "Alice"), ("home", "alice")} in found
        # first.last on the node joins the flast home.
        assert {("experimenter", "carol.jones"), ("home", "cjones")} in found
        actions.create_person(
            cur,
            WHO,
            "Alice",
            [("experimenter", "alice"), ("experimenter", "Alice"), ("home", "alice")],
        )
        assert not any(("home", "alice") in g for g in groups())  # fully assigned groups drop out

    def test_initial_key(self):
        assert suggest.initial_key("grace.hopper") == suggest.alias_key("ghopper")
        assert suggest.initial_key("mary.ann.evans") == "mannevans"
        assert suggest.initial_key("alice") is None

    def test_folder_suggestions(self, both, homes_root, tmp_path):
        census, cur = both
        found = suggest.folder_suggestions(census, min_files=1)
        folders = {s["relative"] for s in found}
        assert "exp1" in folders and "flies.xlsx" not in folders
        label = actions.create_label(cur, WHO, "exp1")
        actions.link(cur, WHO, label, "folder", [os.path.join(homes_root, "alice", "exp1")])
        assert "exp1" not in {
            s["relative"] for s in suggest.folder_suggestions(census, min_files=1)
        }


ORIGIN = {"X-Forwarded-Email": WHO, "Origin": "http://testserver"}


class TestCurationPages:
    @pytest.fixture
    def client(self, census_path):
        return TestClient(create_app(Settings(census_path)))

    def test_edits_need_same_origin(self, client):
        response = client.post(
            "/labels",
            data={"name": "x"},
            headers={"X-Forwarded-Email": WHO},
            follow_redirects=False,
        )
        assert response.status_code == 403
        evil = {"X-Forwarded-Email": WHO, "Origin": "http://evil.example"}
        assert (
            client.post(
                "/labels", data={"name": "x"}, headers=evil, follow_redirects=False
            ).status_code
            == 403
        )

    def test_label_a_selection_then_browse_and_undo(self, client):
        made = client.post(
            "/labels",
            data={"name": "januaries", "kind": "project"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert made.status_code == 303 and made.headers["location"].startswith("/labels/")
        applied = client.post(
            "/labels/apply",
            data={
                "scope": "run",
                "selection": "month=2024-01",
                "new_name": "januaries",
                "back": "/runs?month=2024-01",
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert "Labelled+3+new+run" in applied.headers["location"]
        page = client.get("/runs?label=januaries", headers=ORIGIN).text
        assert "<strong>3</strong> runs" in page and 'class="tag project"' in page
        labels_page = client.get("/labels", headers=ORIGIN).text
        assert "januaries" in labels_page
        changes = client.get("/changes", headers=ORIGIN).text
        assert "labelled 3 run(s)" in changes and WHO in changes
        conn = store.connect(client.app.state.settings.curation_path)
        audit_id = conn.execute("SELECT max(id) FROM audit").fetchone()[0]
        conn.close()
        undone = client.post(f"/changes/{audit_id}/undo", headers=ORIGIN, follow_redirects=False)
        assert undone.status_code == 303
        assert "<strong>0</strong> runs" in client.get("/runs?label=januaries", headers=ORIGIN).text

    def test_people_from_suggestions(self, client):
        page = client.get("/people", headers=ORIGIN).text
        assert "names that probably belong together" in page and "alice" in page
        client.post(
            "/people",
            data={"name": "Alice A", "alias": ["experimenter:alice", "home:alice"]},
            headers=ORIGIN,
        )
        people = client.get("/people", headers=ORIGIN).text
        assert "Alice A" in people
        runs = client.get("/runs?person=Alice+A", headers=ORIGIN).text
        assert "<strong>4</strong> runs" in runs and 'class="who"' in runs
        conn = store.connect(client.app.state.settings.curation_path)
        person_id = conn.execute("SELECT id FROM person").fetchone()[0]
        conn.close()
        person = client.get(f"/people/{person_id}", headers=ORIGIN).text
        assert "Alice A" in person and "<svg" in person

    def test_note_on_a_run_and_label_suggestions_page(self, client, census_path):
        conn = census_store.connect(census_path)
        run_path, run_id = conn.execute("SELECT path, id FROM db_summary LIMIT 1").fetchone()
        conn.close()
        back = f"/runs/{run_id}"
        client.post(
            "/notes",
            data={"target_type": "run", "target": run_path, "text": "lights off", "back": back},
            headers=ORIGIN,
        )
        assert "lights off" in client.get(back, headers=ORIGIN).text
        assert (
            "Folders that look like projects"
            in client.get("/labels/suggestions", headers=ORIGIN).text
        )

    def test_nothing_is_folded_away(self, client, census_path):
        conn = census_store.connect(census_path)
        run_id, file_id = (
            conn.execute("SELECT min(id) FROM db_summary").fetchone()[0],
            conn.execute("SELECT min(id) FROM file_summary").fetchone()[0],
        )
        conn.close()
        for url in (
            "/",
            "/runs",
            "/files",
            "/people",
            "/labels",
            f"/runs/{run_id}",
            f"/files/{file_id}",
        ):
            page = client.get(url, headers=ORIGIN).text
            assert "<details" not in page and "data-toggle-cols" not in page, url
        runs = client.get("/runs", headers=ORIGIN).text
        # Every column and filter is there: the former extras and secondary filters too.
        assert "<th>Location</th>" in runs and "Camera stack" in runs and "Label these" in runs


class TestPeopleFormRegression:
    """The suggestion form has two buttons; "Create person" must not need a chosen person."""

    @pytest.fixture
    def client(self, census_path):
        return TestClient(create_app(Settings(census_path)))

    def test_group_form_has_no_required_picker(self, client):
        client.post("/people", data={"name": "Someone"}, headers=ORIGIN)  # makes the picker appear
        page = client.get("/people", headers=ORIGIN).text
        assert 'name="person_id"' in page
        assert not re.search(r'<select name="person_id"[^>]*required', page)

    def test_add_without_a_person_is_a_friendly_error(self, client):
        response = client.post(
            "/people/join",
            data={"person_id": "", "alias": ["home:alice"]},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert response.status_code == 303 and "choose+the+person" in response.headers["location"]
