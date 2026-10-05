"""Faceted, paginated tables over the census summary tables, built in SQL.

A :class:`Table` describes one summary table: its facets (filterable columns
shown with counts), the columns free text searches, and the allowed sorts. A
:class:`Selection` is what the user picked, parsed from the query string, so
every view of a table is a plain, bookmarkable URL.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlencode

NONE = "(none)"  # facet value for NULL / empty
PER_PAGE = 50


@dataclass(frozen=True)
class Facet:
    """A filterable column: ``param`` in the URL, ``column`` in SQL."""

    param: str
    column: str
    label: str
    limit: int = 12
    by_value: bool = False  # list values in order (years) rather than by count


@dataclass(frozen=True)
class Table:
    """A summary table the UI can browse."""

    name: str
    facets: tuple[Facet, ...]
    text_columns: tuple[str, ...]
    sorts: dict[str, str]
    default_sort: str
    default_desc: bool = True

    def facet(self, param: str) -> Facet | None:
        return next((f for f in self.facets if f.param == param), None)


@dataclass
class Selection:
    """Filters, search text, sort and page chosen by the user."""

    filters: dict[str, list[str]] = field(default_factory=dict)
    q: str = ""
    sort: str = ""
    desc: bool = True
    page: int = 1

    @classmethod
    def from_items(cls, table: Table, items: Iterable[tuple[str, str]]) -> "Selection":
        """Parse ``request.query_params.multi_items()``; unknown params are ignored."""
        selection = cls(sort=table.default_sort, desc=table.default_desc)
        for key, value in items:
            if table.facet(key) and value != "":
                selection.filters.setdefault(key, [])
                if value not in selection.filters[key]:
                    selection.filters[key].append(value)
            elif key == "q":
                selection.q = value.strip()
            elif key == "sort" and value.lstrip("-") in table.sorts:
                selection.desc = value.startswith("-")
                selection.sort = value.lstrip("-")
            elif key == "page" and value.isdigit():
                selection.page = max(1, int(value))
        return selection

    def items(self, **changes: Any) -> list[tuple[str, str]]:
        """Query-string items for this selection with some parts changed."""
        filters = {k: list(v) for k, v in self.filters.items()}
        for key, value in changes.items():
            if key in ("q", "sort", "page"):
                continue
            filters[key] = [] if value is None else [value]
        items = [(k, v) for k, vs in filters.items() for v in vs]
        q = changes.get("q", self.q)
        if q:
            items.append(("q", q))
        sort = changes.get("sort", ("-" if self.desc else "") + self.sort)
        items.append(("sort", sort))
        page = changes.get("page", 1)
        if page and page != 1:
            items.append(("page", str(page)))
        return items

    def url(self, path: str, **changes: Any) -> str:
        return f"{path}?{urlencode(self.items(**changes))}"

    def toggle_url(self, path: str, param: str, value: str) -> str:
        """URL adding ``value`` to (or removing it from) facet ``param``."""
        values = list(self.filters.get(param, []))
        values.remove(value) if value in values else values.append(value)
        filters = {**self.filters, param: values}
        items = [(k, v) for k, vs in filters.items() for v in vs]
        if self.q:
            items.append(("q", self.q))
        items.append(("sort", ("-" if self.desc else "") + self.sort))
        return f"{path}?{urlencode(items)}"


def where(table: Table, selection: Selection, skip: str | None = None) -> tuple[str, list]:
    """SQL WHERE clause (with leading ``WHERE`` or empty) and its parameters.

    Args:
        skip: A facet param to leave out, so its own counts show alternatives.
    """
    clauses, params = [], []
    for param, values in selection.filters.items():
        facet = table.facet(param)
        if facet is None or param == skip or not values:
            continue
        parts = []
        real = [v for v in values if v != NONE]
        if real:
            parts.append(f"{facet.column} IN ({', '.join('?' * len(real))})")
            params += real
        if NONE in values:
            parts.append(f"({facet.column} IS NULL OR {facet.column} = '')")
        clauses.append("(" + " OR ".join(parts) + ")")
    if selection.q and table.text_columns:
        like = f"%{selection.q}%"
        clauses.append("(" + " OR ".join(f"{c} LIKE ?" for c in table.text_columns) + ")")
        params += [like] * len(table.text_columns)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def facet_counts(
    conn: sqlite3.Connection, table: Table, selection: Selection
) -> dict[str, list[dict[str, Any]]]:
    """For each facet, its most frequent values under the other filters."""
    out = {}
    for facet in table.facets:
        clause, params = where(table, selection, skip=facet.param)
        order = "value DESC" if facet.by_value else "n DESC, value"
        rows = conn.execute(
            f"SELECT coalesce(nullif({facet.column}, ''), ?) AS value, count(*) AS n "
            f"FROM {table.name} {clause} GROUP BY value ORDER BY {order} LIMIT ?",
            [NONE, *params, facet.limit],
        ).fetchall()
        chosen = selection.filters.get(facet.param, [])
        values = [
            {"value": str(r["value"]), "n": r["n"], "on": str(r["value"]) in chosen} for r in rows
        ]
        shown = {v["value"] for v in values}
        values += [{"value": v, "n": 0, "on": True} for v in chosen if v not in shown]
        out[facet.param] = values
    return out


def fetch_page(
    conn: sqlite3.Connection,
    table: Table,
    selection: Selection,
    columns: str = "*",
    per_page: int = PER_PAGE,
) -> tuple[list[sqlite3.Row], int]:
    """One page of rows and the total number of rows matching the selection."""
    clause, params = where(table, selection)
    total = conn.execute(f"SELECT count(*) FROM {table.name} {clause}", params).fetchone()[0]
    order = table.sorts.get(selection.sort, table.sorts[table.default_sort])
    direction = "DESC" if selection.desc else "ASC"
    rows = conn.execute(
        f"SELECT {columns} FROM {table.name} {clause} "
        f"ORDER BY {order} {direction} NULLS LAST, id LIMIT ? OFFSET ?",
        [*params, per_page, (selection.page - 1) * per_page],
    ).fetchall()
    return rows, total


def selected_ids(conn: sqlite3.Connection, table: Table, selection: Selection) -> list[int]:
    """Ids of every row matching the selection (for exports and bulk actions)."""
    clause, params = where(table, selection)
    return [r[0] for r in conn.execute(f"SELECT id FROM {table.name} {clause}", params)]
