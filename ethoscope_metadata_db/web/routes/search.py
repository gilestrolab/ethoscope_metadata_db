"""Search: one box across machines, dates, people, files, runs and genotypes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from .. import queries
from ..app import get_db, render

router = APIRouter()


@router.get("/search")
def search(request: Request, q: str = "", conn: sqlite3.Connection = Depends(get_db)):
    return render(request, "search.html", q=q, results=queries.search(conn, q))
