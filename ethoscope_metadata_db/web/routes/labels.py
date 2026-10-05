"""Labels and projects: list, create, edit, merge, delete, apply to selections, folder suggestions."""

from __future__ import annotations

import sqlite3
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .. import charts, queries
from ..app import back_to, current_user, get_curation, get_db, refresh_membership, render
from ..curation import actions, suggest, views
from ..curation.store import CurationError
from ..filters import Selection, selected_ids

router = APIRouter()


@router.get("/labels")
def labels(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return render(request, "labels.html", labels=views.all_labels(conn))


@router.post("/labels")
def create(
    request: Request,
    name: str = Form(...),
    kind: str = Form("tag"),
    doi: str = Form(""),
    description: str = Form(""),
    authors: str = Form(""),
    cur: sqlite3.Connection = Depends(get_curation),
):
    try:
        label_id = actions.create_label(
            cur, current_user(request), name, kind, doi, description, authors
        )
    except CurationError as exc:
        return back_to("/labels", error=str(exc))
    return back_to(f"/labels/{label_id}", notice="Label created")


@router.get("/labels/suggestions")
def suggestions(request: Request, q: str = "", conn: sqlite3.Connection = Depends(get_db)):
    found = suggest.folder_suggestions(conn)
    if q:
        found = [s for s in found if q.lower() in s["folder"].lower()]
    return render(request, "label_suggestions.html", suggestions=found[:200], q=q)


@router.post("/labels/suggestions/accept")
def accept_suggestion(
    request: Request,
    folder: str = Form(...),
    name: str = Form(...),
    kind: str = Form("project"),
    cur: sqlite3.Connection = Depends(get_curation),
):
    who = current_user(request)
    try:
        existing = cur.execute("SELECT id FROM label WHERE name = ?", (name.strip(),)).fetchone()
        label_id = existing["id"] if existing else actions.create_label(cur, who, name, kind)
        actions.link(cur, who, label_id, "folder", [folder])
    except CurationError as exc:
        return back_to("/labels/suggestions", error=str(exc))
    refresh_membership(request, cur)
    return back_to("/labels/suggestions", notice=f"Labelled the folder {name!r}")


@router.post("/labels/apply")
def apply(
    request: Request,
    scope: str = Form(...),
    selection: str = Form(""),
    target: str = Form(""),
    label_id: str = Form(""),
    new_name: str = Form(""),
    back: str = Form("/"),
    census: sqlite3.Connection = Depends(get_db),
    cur: sqlite3.Connection = Depends(get_curation),
):
    """Label a whole Runs/Files selection (``selection`` = its query string) or one ``target``."""
    who = current_user(request)
    try:
        chosen = _label_for(cur, who, label_id, new_name)
        if target:
            targets, target_type = [target], scope
        else:
            table = queries.RUNS if scope == "run" else queries.FILES
            sel = Selection.from_items(table, parse_qsl(selection))
            ids = selected_ids(census, table, sel)
            if not ids:
                raise CurationError("the selection is empty")
            if len(ids) > 20000:
                raise CurationError("selections above 20,000 items are refused: narrow the filters")
            targets = _paths(census, table.name, ids)
            target_type = scope
        added = actions.link(cur, who, chosen, target_type, targets)
    except CurationError as exc:
        return back_to(back, error=str(exc))
    refresh_membership(request, cur)
    return back_to(back, notice=f"Labelled {added} new {target_type}(s)")


def _label_for(cur: sqlite3.Connection, who: str | None, label_id: str, new_name: str) -> int:
    if new_name.strip():
        existing = cur.execute(
            "SELECT id FROM label WHERE name = ?", (new_name.strip(),)
        ).fetchone()
        return existing["id"] if existing else actions.create_label(cur, who, new_name)
    if label_id.isdigit():
        return int(label_id)
    raise CurationError("choose a label or type a new name")


def _paths(conn: sqlite3.Connection, table: str, ids: list[int]) -> list[str]:
    paths = []
    for start in range(0, len(ids), 900):
        chunk = ids[start : start + 900]
        paths += [
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT path FROM {table} WHERE id IN ({', '.join('?' * len(chunk))})",
                chunk,
            )
        ]
    return paths


@router.get("/labels/{label_id}")
def label(request: Request, label_id: int, conn: sqlite3.Connection = Depends(get_db)):
    detail = views.label_detail(conn, label_id)
    if detail is None:
        raise HTTPException(404, "No such label")
    chart = charts.monthly_columns(
        detail["monthly"],
        link=lambda m: f"/runs?label={detail['label']['name']}&month={m}",
        height=180,
    )
    others = [l for l in views.all_labels(conn) if l["id"] != label_id]
    return render(request, "label.html", **detail, chart=chart, others=others, fold=charts.fold)


@router.post("/labels/{label_id}/edit")
def edit(
    request: Request,
    label_id: int,
    name: str = Form(...),
    kind: str = Form("tag"),
    doi: str = Form(""),
    description: str = Form(""),
    authors: str = Form(""),
    cur: sqlite3.Connection = Depends(get_curation),
):
    try:
        actions.edit_label(
            cur,
            current_user(request),
            label_id,
            name=name,
            kind=kind,
            doi=doi,
            description=description,
            authors=authors,
        )
    except CurationError as exc:
        return back_to(f"/labels/{label_id}", error=str(exc))
    return back_to(f"/labels/{label_id}", notice="Saved")


@router.post("/labels/{label_id}/merge")
def merge(
    request: Request,
    label_id: int,
    into: int = Form(...),
    cur: sqlite3.Connection = Depends(get_curation),
):
    try:
        actions.merge_labels(cur, current_user(request), label_id, into)
    except CurationError as exc:
        return back_to(f"/labels/{label_id}", error=str(exc))
    refresh_membership(request, cur)
    return back_to(f"/labels/{into}", notice="Labels merged")


@router.post("/labels/{label_id}/delete")
def delete(request: Request, label_id: int, cur: sqlite3.Connection = Depends(get_curation)):
    try:
        actions.delete_label(cur, current_user(request), label_id)
    except CurationError as exc:
        return back_to(f"/labels/{label_id}", error=str(exc))
    refresh_membership(request, cur)
    return back_to("/labels", notice="Label deleted (undo it from Changes)")


@router.post("/labels/{label_id}/unlink")
def unlink(
    request: Request,
    label_id: int,
    target_type: str = Form(...),
    target: str = Form(...),
    back: str = Form(""),
    cur: sqlite3.Connection = Depends(get_curation),
):
    try:
        actions.unlink(cur, current_user(request), label_id, target_type, [target])
    except CurationError as exc:
        return back_to(back or f"/labels/{label_id}", error=str(exc))
    refresh_membership(request, cur)
    return back_to(back or f"/labels/{label_id}", notice="Label removed")
