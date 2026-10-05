import sqlite3
from urllib.parse import parse_qsl, urlsplit

import pytest

from ethoscope_metadata_db.web import charts, filters
from ethoscope_metadata_db.web.queries import RUNS


def _items(url):
    return parse_qsl(urlsplit(url).query)


class TestSelection:
    def test_parse_ignores_unknown_and_keeps_repeats(self):
        sel = filters.Selection.from_items(
            RUNS,
            [
                ("category", "orphan"),
                ("category", "misfire"),
                ("category", "orphan"),
                ("evil", "1"),
                ("sort", "-size"),
                ("page", "3"),
                ("q", " 102 "),
            ],
        )
        assert sel.filters == {"category": ["orphan", "misfire"]}
        assert (sel.sort, sel.desc, sel.page, sel.q) == ("size", True, 3, "102")

    def test_bad_sort_falls_back_to_default(self):
        sel = filters.Selection.from_items(RUNS, [("sort", "path; DROP TABLE x")])
        assert (sel.sort, sel.desc) == ("start", True)

    def test_toggle_and_page_urls(self):
        sel = filters.Selection.from_items(RUNS, [("category", "orphan"), ("page", "2")])
        on = _items(sel.toggle_url("/runs", "year", "2024"))
        assert ("year", "2024") in on and ("category", "orphan") in on
        assert ("page", "2") not in on  # changing a filter goes back to page 1
        off = _items(sel.toggle_url("/runs", "category", "orphan"))
        assert ("category", "orphan") not in off
        assert ("page", "3") in _items(sel.url("/runs", page=3))


@pytest.fixture
def db(tmp_path):
    from ethoscope_metadata_db.web.curation import store

    store.connect(tmp_path / "cur.sqlite").close()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS cur", (str(tmp_path / "cur.sqlite"),))
    conn.execute(
        "CREATE TABLE db_summary (id INTEGER, path TEXT, machine_name TEXT, experimenter TEXT, "
        "location TEXT, homes TEXT, category TEXT, year TEXT, month TEXT, start_local TEXT, "
        "stack TEXT, interactor TEXT, n_rois INTEGER, n_rows INTEGER, n_sources INTEGER, "
        "size INTEGER, duration_h REAL)"
    )
    rows = [
        (
            1,
            "/a",
            "ETHOSCOPE_001",
            "ann",
            None,
            None,
            "orphan",
            "2024",
            "2024-01",
            "2024-01-02",
            "legacy",
            None,
            20,
            0,
            0,
            10,
            None,
        ),
        (
            2,
            "/b",
            "ETHOSCOPE_001",
            "",
            None,
            None,
            "single",
            "2024",
            "2024-02",
            "2024-02-02",
            None,
            None,
            20,
            5,
            1,
            30,
            70.0,
        ),
        (
            3,
            "/c",
            "ETHOSCOPE_002",
            "bob",
            None,
            None,
            "orphan",
            "2025",
            "2025-01",
            "2025-01-02",
            "legacy",
            None,
            10,
            0,
            0,
            20,
            30.0,
        ),
    ]
    conn.executemany(f"INSERT INTO db_summary VALUES ({', '.join('?' * 17)})", rows)
    return conn


class TestSql:
    def test_facets_count_alternatives(self, db):
        sel = filters.Selection.from_items(RUNS, [("category", "orphan")])
        counts = filters.facet_counts(db, RUNS, sel)
        # The category facet ignores its own filter, so both values show.
        assert {v["value"]: v["n"] for v in counts["category"]} == {"orphan": 2, "single": 1}
        assert {v["value"]: v["n"] for v in counts["year"]} == {"2024": 1, "2025": 1}
        assert counts["month"] == []  # hidden facet

    def test_none_value_matches_null_and_empty(self, db):
        sel = filters.Selection.from_items(RUNS, [("experimenter", filters.NONE)])
        rows, total = filters.fetch_page(db, RUNS, sel)
        assert total == 1 and rows[0]["id"] == 2

    def test_page_sort_and_text(self, db):
        sel = filters.Selection.from_items(RUNS, [("sort", "size"), ("q", "ETHOSCOPE_001")])
        rows, total = filters.fetch_page(db, RUNS, sel, per_page=1)
        assert total == 2 and rows[0]["id"] == 1
        assert filters.selected_ids(db, RUNS, sel) == [1, 2]

    def test_sql_injection_in_values_is_inert(self, db):
        sel = filters.Selection.from_items(RUNS, [("machine", "x' OR '1'='1")])
        assert filters.fetch_page(db, RUNS, sel)[1] == 0


class TestCharts:
    def test_nice_ticks(self):
        assert charts.nice_ticks(0) == [0, 1]
        assert charts.nice_ticks(370) == [0, 100, 200, 300, 400]
        assert charts.nice_ticks(7) == [0, 2, 4, 6, 8]

    def test_monthly_columns_fill_gaps_and_link(self):
        svg = charts.monthly_columns(
            {"2024-01": {"single": 3, "orphan": 1}, "2024-03": {"misfire": 2}},
            link=lambda m: f"/runs?month={m}",
        )
        assert svg.count('class="col"') == 3  # February is drawn, empty
        assert "/runs?month=2024-02" in svg and "Jan 2024: 4 databases" in svg
        assert 'class="s-claimed"' in svg and "s-failed" in svg
        assert "No dated runs" in charts.monthly_columns({}, link=str)

    def test_fold_and_legend(self):
        assert charts.fold({"single": 1, "multiple": 2, "empty": 1})["claimed"] == 3
        assert charts.legend().count("<li>") == len(charts.SERIES)


def test_chart_leaves_out_unset_clocks():
    monthly = {"1970-01": {"orphan": 2}, "2024-01": {"single": 1}}
    svg = charts.monthly_columns(monthly, link=lambda m: m)
    assert "1970" not in svg and svg.count('class="col"') == 1
    assert charts.implausible(monthly) == 2
