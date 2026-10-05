"""The audit log with undo, and notes on runs, files, labels and people."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request

from ..app import back_to, current_user, get_curation, get_db, refresh_membership, render
from ..curation import actions, store, views

router = APIRouter()


@router.get("/changes")
def changes(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return render(request, "changes.html", changes=views.recent_changes(conn))


@router.post("/changes/{audit_id}/undo")
def undo(request: Request, audit_id: int, cur: sqlite3.Connection = Depends(get_curation)):
    try:
        entry = store.undo(cur, audit_id, current_user(request))
    except store.CurationError as exc:
        return back_to("/changes", error=str(exc))
    refresh_membership(request, cur)
    return back_to("/changes", notice=f"Undone: {entry['summary']}")


@router.post("/notes")
def add_note(
    request: Request,
    target_type: str = Form(...),
    target: str = Form(...),
    text: str = Form(...),
    back: str = Form("/"),
    cur: sqlite3.Connection = Depends(get_curation),
):
    try:
        actions.add_note(cur, current_user(request), target_type, target, text)
    except store.CurationError as exc:
        return back_to(back, error=str(exc))
    return back_to(back, notice="Note added")


@router.post("/notes/{note_id}/delete")
def delete_note(
    request: Request,
    note_id: int,
    back: str = Form("/"),
    cur: sqlite3.Connection = Depends(get_curation),
):
    actions.delete_note(cur, current_user(request), note_id)
    return back_to(back, notice="Note deleted")
