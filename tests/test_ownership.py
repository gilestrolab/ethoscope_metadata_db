"""Ownership: each run and metadata file maps to at most one real, unambiguous person.

The test census (see ``test_curation.census_path``) has runs 1-4 typed
``alice`` on the node; run 1 is claimed by alice's and bob's metadata, run 3
by bob's and carol's, run 4 by alice's only.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_curation import ORIGIN, WHO, both, census_path, cur  # noqa: F401 (fixtures)

from ethoscope_metadata_db.web.app import Settings, create_app
from ethoscope_metadata_db.web.curation import actions, membership, store, suggest, views
from ethoscope_metadata_db.web.curation.store import CurationError


def owners(census):
    """``{run id: (owner name, via)}`` after a rebuild."""
    return {
        rid: (name, via)
        for rid, name, via in census.execute(
            "SELECT m.target_id, p.name, m.via FROM cur.run_person m "
            "JOIN cur.person p ON p.id = m.person_id"
        )
    }


@pytest.fixture
def no_experimenter(both):
    """The census with no experimenter typed on any node, so owners come from metadata."""
    census, cur = both
    census.execute("UPDATE db_summary SET experimenter = NULL")
    census.commit()
    return census, cur


class TestRunOwners:
    def test_one_real_owner_among_claiming_files(self, no_experimenter):
        census, cur = no_experimenter
        actions.create_person(cur, WHO, "Bob", [("home", "bob")])
        actions.create_person(cur, WHO, "Carol", [("home", "carol")])
        membership.rebuild(census, cur)
        # Run 1: alice's file has no owner yet, so it is neutral and Bob owns the run.
        # Run 3: Bob and Carol both claim it, so nobody does.
        assert owners(census) == {1: ("Bob", "metadata")}
        actions.create_person(cur, WHO, "Alice", [("home", "alice")])
        membership.rebuild(census, cur)
        assert owners(census) == {4: ("Alice", "metadata")}  # run 1 is now ambiguous too

    def test_not_a_person_owns_nothing(self, no_experimenter):
        census, cur = no_experimenter
        actions.create_person(cur, WHO, "Bob", [("home", "bob")])
        actions.create_person(cur, WHO, "carol (shared)", [("home", "carol")], kind="shared")
        membership.rebuild(census, cur)
        assert owners(census) == {1: ("Bob", "metadata"), 3: ("Bob", "metadata")}
        homes = {
            home
            for (home,) in census.execute(
                "SELECT f.home FROM cur.file_person m JOIN file_summary f ON f.id = m.target_id"
            )
        }
        assert homes == {"bob"}

    def test_experimenter_name_wins_over_metadata(self, both):
        census, cur = both
        actions.create_person(cur, WHO, "Alice", [("experimenter", "alice")])
        actions.create_person(cur, WHO, "Bob", [("home", "bob")])
        membership.rebuild(census, cur)
        assert owners(census) == {rid: ("Alice", "experimenter") for rid in (1, 2, 3, 4)}

    def test_coverage(self, both):
        census, cur = both
        assert views.coverage(census)["owned_runs"] == 0
        actions.create_person(cur, WHO, "Alice", [("experimenter", "alice"), ("home", "alice")])
        membership.rebuild(census, cur)
        cover = views.coverage(census)
        assert (cover["runs"], cover["owned_runs"], cover["runs_pct"]) == (6, 4, 67)
        # Only alice's real metadata sheet counts, not her rejected notes.
        assert (cover["files"], cover["owned_files"]) == (3, 1)
        assert cover["via"] == {"experimenter": 4}


class TestKinds:
    def test_set_kind_and_undo(self, cur):
        person = actions.create_person(cur, WHO, "guest", [("home", "guest")])
        actions.set_person_kind(cur, WHO, person, "shared")
        assert cur.execute("SELECT kind FROM person").fetchone()[0] == "shared"
        store.undo(cur, cur.execute("SELECT max(id) FROM audit").fetchone()[0], WHO)
        assert cur.execute("SELECT kind FROM person").fetchone()[0] == "person"

    def test_bad_kind(self, cur):
        with pytest.raises(CurationError):
            actions.create_person(cur, WHO, "X", kind="team")
        person = actions.create_person(cur, WHO, "Y")
        with pytest.raises(CurationError):
            actions.set_person_kind(cur, WHO, person, "")

    def test_suggestions_never_join_a_not_a_person(self, both):
        census, cur = both
        actions.create_person(cur, WHO, "Guests", [("home", "alice")], kind="shared")
        group = next(
            g
            for g in suggest.alias_groups(census)
            if ("experimenter", "alice") in {(m["kind"], m["value"]) for m in g["members"]}
        )
        assert group["people"] == [] and group["suggested"] == "alice"

    def test_new_rules_rebuild_existing_files(self, both, monkeypatch):
        census, cur = both
        assert membership.ensure_fresh(census, cur) is True
        assert membership.ensure_fresh(census, cur) is False
        monkeypatch.setattr(membership, "RULES_VERSION", "next")
        assert membership.ensure_fresh(census, cur) is True

    def test_old_curation_file_is_upgraded(self, tmp_path):
        path = tmp_path / "old.sqlite"
        old = sqlite3.connect(path)
        old.executescript(
            "CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, note TEXT);"
            "INSERT INTO person (name) VALUES ('Alice');"
            "CREATE TABLE run_person (target_id INTEGER NOT NULL, person_id INTEGER NOT NULL);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO meta VALUES ('built_for', 'then');"
        )
        old.commit()
        old.close()
        conn = store.connect(path)
        assert tuple(
            conn.execute("SELECT kind, username, role, active FROM person").fetchone()
        ) == ("person", None, None, 0)
        assert "via" in {r[1] for r in conn.execute("PRAGMA table_info(run_person)")}
        assert conn.execute("SELECT value FROM meta WHERE key = 'built_for'").fetchone() is None
        conn.close()


class TestPersonDetails:
    def test_full_name_username_and_role(self, cur):
        person = actions.create_person(
            cur, WHO, "Ada Lovelace", [("home", "alovelace")], username="alovelace"
        )
        actions.edit_person(cur, WHO, person, "Ada Lovelace", "alovelace", " PhD  student ")
        row = cur.execute("SELECT name, username, role FROM person").fetchone()
        assert tuple(row) == ("Ada Lovelace", "alovelace", "PhD student")
        actions.edit_person(cur, WHO, person, "Ada Lovelace", "", "")  # blanks clear
        assert tuple(cur.execute("SELECT username, role FROM person").fetchone()) == (None, None)
        store.undo(cur, cur.execute("SELECT max(id) FROM audit").fetchone()[0], WHO)
        assert cur.execute("SELECT role FROM person").fetchone()[0] == "PhD student"

    def test_in_the_lab_or_alumni(self, cur):
        alumna = actions.create_person(cur, WHO, "Ada Lovelace")
        current = actions.create_person(cur, WHO, "Rosalind Franklin", active=True)
        team = actions.create_person(cur, WHO, "infection team", kind="shared", active=True)
        active = dict(cur.execute("SELECT id, active FROM person").fetchall())
        assert active == {alumna: 0, current: 1, team: 0}  # a not-a-person is never "in the lab"
        actions.edit_person(cur, WHO, current, "Rosalind Franklin", "rfranklin", "postdoc", active=False)
        assert cur.execute("SELECT active FROM person WHERE id = ?", (current,)).fetchone()[0] == 0
        store.undo(cur, cur.execute("SELECT max(id) FROM audit").fetchone()[0], WHO)
        assert cur.execute("SELECT active FROM person WHERE id = ?", (current,)).fetchone()[0] == 1

    def test_usernames_are_unique_and_account_like(self, cur):
        actions.create_person(cur, WHO, "Ada Lovelace", username="alovelace")
        with pytest.raises(CurationError, match="already belongs to 'Ada Lovelace'"):
            actions.create_person(cur, WHO, "Someone Else", username="ALovelace")
        other = actions.create_person(cur, WHO, "Someone Else")
        with pytest.raises(CurationError, match="letters, digits"):
            actions.edit_person(cur, WHO, other, "Someone Else", "some one")
        # Many people may have no username at all.
        actions.create_person(cur, WHO, "A Third")
        assert cur.execute("SELECT count(*) FROM person WHERE username IS NULL").fetchone()[0] == 2


class TestPeoplePage:
    @pytest.fixture
    def client(self, census_path):
        return TestClient(create_app(Settings(census_path)))

    def test_review_queue(self, client):
        page = client.get("/people", headers=ORIGIN).text
        assert "0</strong> of 6 runs have an owner" in page
        assert "To review: names that probably belong together" in page
        assert 'name="kind" value="shared"' in page  # the "Not a person" button
        client.post(
            "/people",
            data={"name": "Alice", "alias": ["experimenter:alice", "home:alice"], "kind": "person"},
            headers=ORIGIN,
        )
        client.post(
            "/people",
            data={"name": "carol's folder", "alias": ["home:carol"], "kind": "shared"},
            headers=ORIGIN,
        )
        page = client.get("/people", headers=ORIGIN).text
        assert "4</strong> of 6 runs have an owner" in page
        assert "Not people (1)" in page and "carol&#39;s folder" in page
        runs = client.get("/runs", headers=ORIGIN).text
        assert 'title="owner: the experimenter name typed on the node"' in runs

    def test_create_and_edit_with_details(self, client):
        blank = client.post(
            "/people",
            data={"name": " ", "alias": ["home:alice"], "kind": "person"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert "full+name" in blank.headers["location"]
        client.post(
            "/people",
            data={"name": "", "suggested": "carol", "alias": ["home:carol"], "kind": "shared"},
            headers=ORIGIN,
        )
        client.post(
            "/people",
            data={"name": "Alice Ant", "alias": ["home:alice"], "username": "alice"},
            headers=ORIGIN,
        )
        conn = store.connect(client.app.state.settings.curation_path)
        names = dict(conn.execute("SELECT name, kind FROM person").fetchall())
        alice = conn.execute("SELECT id FROM person WHERE username = 'alice'").fetchone()[0]
        conn.close()
        assert names == {"carol": "shared", "Alice Ant": "person"}
        assert "Former lab member · <span" in client.get(f"/people/{alice}", headers=ORIGIN).text
        client.post(
            f"/people/{alice}/edit",
            data={"name": "Alice Antonelli", "username": "alice", "role": "postdoc"},
            headers=ORIGIN,
        )
        page = client.get("/people", headers=ORIGIN).text
        assert "Alice Antonelli" in page and "<td>postdoc</td>" in page
        assert '<option value="PhD student">' in page  # role choices
        assert "In the lab (0)" in page and "Alumni (1)" in page
        client.post(
            f"/people/{alice}/edit",
            data={"name": "Alice Antonelli", "username": "alice", "role": "postdoc", "active": "1"},
            headers=ORIGIN,
        )
        page = client.get("/people", headers=ORIGIN).text
        assert "In the lab (1)" in page and "Alumni (0)" in page
        assert "In the lab · <span" in client.get(f"/people/{alice}", headers=ORIGIN).text

    def test_switch_kind_from_the_person_page(self, client):
        client.post("/people", data={"name": "guest", "alias": ["home:carol"]}, headers=ORIGIN)
        conn = store.connect(client.app.state.settings.curation_path)
        person_id = conn.execute("SELECT id FROM person").fetchone()[0]
        conn.close()
        assert "Not a person</button>" in client.get(f"/people/{person_id}", headers=ORIGIN).text
        client.post(f"/people/{person_id}/kind", data={"kind": "shared"}, headers=ORIGIN)
        page = client.get(f"/people/{person_id}", headers=ORIGIN).text
        assert "Marked as not a person" in page and "This is a person</button>" in page
        response = client.post(
            f"/people/{person_id}/kind",
            data={"kind": "robot"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert "error=" in response.headers["location"]
