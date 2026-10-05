"""Runs (databases): faceted list, a run's page, and CSV exports of a selection."""

from __future__ import annotations

import math
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import queries
from ..app import get_db, render
from ..curation import views
from ..filters import PER_PAGE, Selection, facet_counts, fetch_page

router = APIRouter()

LIST_COLUMNS = (
    "id, path, machine_name, start_local, experimenter, location, stack, interactor, "
    "n_rois, size, duration_h, category, n_rows, n_sources, homes"
)


def _selection(request: Request) -> Selection:
    return Selection.from_items(queries.RUNS, request.query_params.multi_items())


def _csv(frame, filename: str) -> Response:
    return Response(
        frame.to_csv(index=False),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runs")
def runs(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    selection = _selection(request)
    rows, total = fetch_page(conn, queries.RUNS, selection, LIST_COLUMNS)
    ids = [r["id"] for r in rows]
    context = {
        "selection": selection,
        "table": queries.RUNS,
        "rows": rows,
        "total": total,
        "pages": max(1, math.ceil(total / PER_PAGE)),
        "facets": facet_counts(conn, queries.RUNS, selection),
        "row_labels": views.labels_by_id(conn, "run_label", ids),
        "row_people": views.people_by_id(conn, "run_person", ids),
        "all_labels": views.all_labels(conn),
        "query": str(request.query_params),
    }
    partial = request.headers.get("HX-Request") == "true"
    return render(request, "_runs_results.html" if partial else "runs.html", **context)


@router.get("/runs/export.csv")
def export_runs(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """The selected runs, one row each."""
    return _csv(queries.selection_runs(conn, _selection(request)), "runs.csv")


@router.get("/runs/ethoscopy.csv")
def export_tubes(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """Uniquely matched tubes of the selected runs, as ethoscopy metadata (+ path)."""
    return _csv(queries.selection_tubes(conn, _selection(request)), "metadata_ethoscopy.csv")


@router.get("/runs/{db_id}")
def run(request: Request, db_id: int, conn: sqlite3.Connection = Depends(get_db)):
    detail = queries.run_detail(conn, db_id)
    if detail is None:
        raise HTTPException(404, "No such database in the census")
    return render(
        request,
        "run.html",
        **detail,
        labels=views.labels_by_id(conn, "run_label", [db_id])[db_id],
        people=views.people_by_id(conn, "run_person", [db_id])[db_id],
        notes=views.notes(conn, "run", detail["run"]["path"]),
        all_labels=views.all_labels(conn),
    )
