"""Overview: headline counts and breakdowns of the whole census."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from .. import charts, queries
from ..app import get_db, render

router = APIRouter()


@router.get("/")
def overview(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    data = queries.overview(conn)
    chart = charts.monthly_columns(data["monthly"], link=lambda month: f"/runs?month={month}")
    return render(
        request,
        "overview.html",
        data=data,
        chart=chart,
        fold=charts.fold,
        left_out=charts.implausible(data["monthly"]),
    )
