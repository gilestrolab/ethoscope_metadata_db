"""Metadata files: faceted list, a file's page, and its tubes as ethoscopy metadata."""

from __future__ import annotations

import math
import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import queries
from ..app import get_db, render
from ..curation import views
from ..filters import PER_PAGE, Selection, facet_counts, fetch_page

router = APIRouter()

LIST_COLUMNS = (
    "id, path, sheet, home, tag, status, outcome, n_rows, n_unique, n_not_found, "
    "n_roi_missing, n_date_ambiguous, n_incomplete, n_dbs, n_copies, date_min, date_max, mtime"
)


def _selection(request: Request) -> Selection:
    items = request.query_params.multi_items()
    selection = Selection.from_items(queries.FILES, items)
    # Reason: most tabular files in the homes are not metadata; show metadata unless asked.
    if "outcome" not in selection.filters and not request.query_params.get("all"):
        selection.filters["outcome"] = list(queries.METADATA_OUTCOMES)
    return selection


@router.get("/files")
def files(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    selection = _selection(request)
    rows, total = fetch_page(conn, queries.FILES, selection, LIST_COLUMNS)
    ids = [r["id"] for r in rows]
    context = {
        "selection": selection,
        "table": queries.FILES,
        "rows": rows,
        "total": total,
        "pages": max(1, math.ceil(total / PER_PAGE)),
        "facets": facet_counts(conn, queries.FILES, selection),
        "row_labels": views.labels_by_id(conn, "file_label", ids),
        "row_people": views.people_by_id(conn, "file_person", ids),
        "all_labels": views.all_labels(conn),
        "query": str(request.query_params),
    }
    partial = request.headers.get("HX-Request") == "true"
    return render(request, "_files_results.html" if partial else "files.html", **context)


@router.get("/files/{file_id}/ethoscopy.csv")
def file_tubes(file_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """The file's uniquely matched tubes, ready for ethoscopy's ``link_meta_index``."""
    row = conn.execute("SELECT path FROM file_summary WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such metadata file in the census")
    name = os.path.splitext(os.path.basename(row["path"]))[0] + "_ethoscopy.csv"
    return Response(
        queries.file_tubes(conn, file_id).to_csv(index=False),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/files/{file_id}")
def file(request: Request, file_id: int, conn: sqlite3.Connection = Depends(get_db)):
    detail = queries.file_detail(conn, file_id)
    if detail is None:
        raise HTTPException(404, "No such metadata file in the census")
    return render(
        request,
        "file.html",
        **detail,
        labels=views.labels_by_id(conn, "file_label", [file_id])[file_id],
        people=views.people_by_id(conn, "file_person", [file_id])[file_id],
        notes=views.notes(conn, "file", detail["file"]["path"]),
        all_labels=views.all_labels(conn),
    )
