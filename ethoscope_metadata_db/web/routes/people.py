"""People: who ran what, merging the names they appear under (homes, experimenter strings)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .. import charts
from ..app import back_to, current_user, get_curation, get_db, refresh_membership, render
from ..curation import actions, suggest, views
from ..curation.store import CurationError

router = APIRouter()


def _parse_aliases(values: list[str]) -> list[tuple[str, str]]:
    """Form values ``kind:value`` -> ``(kind, value)``."""
    out = []
    for item in values:
        kind, _, value = item.partition(":")
        if value:
            out.append((kind, value))
    return out


@router.get("/people")
def people(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return render(
        request,
        "people.html",
        people=views.all_people(conn),
        groups=suggest.alias_groups(conn),
    )


@router.post("/people")
def create(
    request: Request,
    name: str = Form(...),
    alias: list[str] = Form(default=[]),
    cur: sqlite3.Connection = Depends(get_curation),
):
    """Create a person from a suggested alias group (or from scratch)."""
    try:
        person_id = actions.create_person(cur, current_user(request), name, _parse_aliases(alias))
    except CurationError as exc:
        return back_to("/people", error=str(exc))
    refresh_membership(request, cur)
    return (
        back_to("/people", notice=f"Added {name.strip()!r}")
        if alias
        else back_to(f"/people/{person_id}")
    )


@router.post("/people/join")
def join(
    request: Request,
    person_id: str = Form(""),
    alias: list[str] = Form(default=[]),
    back: str = Form("/people"),
    cur: sqlite3.Connection = Depends(get_curation),
):
    """Give an existing person more aliases (e.g. a suggested group)."""
    try:
        if not person_id.isdigit():
            raise CurationError("choose the person to add these names to")
        actions.add_aliases(cur, current_user(request), int(person_id), _parse_aliases(alias))
    except CurationError as exc:
        return back_to(back, error=str(exc))
    refresh_membership(request, cur)
    return back_to(back, notice="Aliases added")


@router.get("/people/{person_id}")
def person(request: Request, person_id: int, conn: sqlite3.Connection = Depends(get_db)):
    detail = views.person_detail(conn, person_id)
    if detail is None:
        raise HTTPException(404, "No such person")
    name = detail["person"]["name"]
    chart = charts.monthly_columns(
        detail["monthly"], link=lambda m: f"/runs?person={name}&month={m}", height=180
    )
    others = [p["person"] for p in views.all_people(conn) if p["person"]["id"] != person_id]
    return render(request, "person.html", **detail, chart=chart, others=others, fold=charts.fold)


@router.post("/people/{person_id}/alias")
def add_alias(
    request: Request,
    person_id: int,
    kind: str = Form(...),
    value: str = Form(...),
    cur: sqlite3.Connection = Depends(get_curation),
):
    try:
        actions.add_aliases(cur, current_user(request), person_id, [(kind, value)])
    except CurationError as exc:
        return back_to(f"/people/{person_id}", error=str(exc))
    refresh_membership(request, cur)
    return back_to(f"/people/{person_id}", notice="Alias added")


@router.post("/people/{person_id}/alias/remove")
def remove_alias(
    request: Request,
    person_id: int,
    kind: str = Form(...),
    value: str = Form(...),
    cur: sqlite3.Connection = Depends(get_curation),
):
    actions.remove_alias(cur, current_user(request), kind, value)
    refresh_membership(request, cur)
    return back_to(f"/people/{person_id}", notice="Alias removed")


@router.post("/people/{person_id}/rename")
def rename(
    request: Request,
    person_id: int,
    name: str = Form(...),
    cur: sqlite3.Connection = Depends(get_curation),
):
    try:
        actions.rename_person(cur, current_user(request), person_id, name)
    except CurationError as exc:
        return back_to(f"/people/{person_id}", error=str(exc))
    return back_to(f"/people/{person_id}", notice="Renamed")


@router.post("/people/{person_id}/merge")
def merge(
    request: Request,
    person_id: int,
    into: int = Form(...),
    cur: sqlite3.Connection = Depends(get_curation),
):
    try:
        actions.merge_people(cur, current_user(request), person_id, into)
    except CurationError as exc:
        return back_to(f"/people/{person_id}", error=str(exc))
    refresh_membership(request, cur)
    return back_to(f"/people/{into}", notice="Merged")


@router.post("/people/{person_id}/delete")
def delete(request: Request, person_id: int, cur: sqlite3.Connection = Depends(get_curation)):
    try:
        actions.delete_person(cur, current_user(request), person_id)
    except CurationError as exc:
        return back_to(f"/people/{person_id}", error=str(exc))
    refresh_membership(request, cur)
    return back_to("/people", notice="Person deleted (undo it from Changes)")
