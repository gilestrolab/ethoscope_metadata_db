"""Read metadata spreadsheets into census rows.

Handles CSV / TSV / TXT (delimiter and encoding sniffed) and XLSX / XLS
(every sheet). What counts as metadata is what ethoscopy accepts: a first
row with the columns ``machine_name``, ``date`` and ``region_id`` (plus an
optional ``time``). Raw cell values are always kept in ``raw_json``.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import sqlite3
from typing import Any

import pandas as pd

from . import store
from .normalise import (
    clean_text,
    is_blank,
    normalise_machine_name,
    normalise_region_id,
    normalise_sex,
    parse_date,
    parse_time,
)

TEXT_EXTENSIONS = {".csv", ".tsv", ".txt"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}

# ethoscopy's ``link_meta_index`` hard-codes these header names: a file is
# usable as ethoscope metadata only if its first row has them, spelled exactly.
REQUIRED_COLUMNS = ("machine_name", "date", "region_id")
# Optional ethoscopy column: start time, as in the results-tree stamp (HH-MM-SS).
TIME_COLUMN = "time"
# Descriptive columns copied to dedicated census fields (case-insensitive);
# every other column is kept verbatim in ``other_json``.
DESCRIPTIVE_COLUMNS = ("genotype", "sex", "treatment")


def map_columns(headers: list[Any]) -> tuple[dict[str, int], list[str]]:
    """Map census fields to column positions in a header row.

    ``machine_name``, ``date``, ``region_id`` and ``time`` must match exactly,
    as ethoscopy requires; ``genotype``, ``sex`` and ``treatment`` match
    case-insensitively.

    Args:
        headers: Header cells in column order.

    Returns:
        ``(mapping, notes)``: field -> column index (first occurrence wins) and
        notes about repeated columns that were not used.
    """
    mapping: dict[str, int] = {}
    notes = []
    for idx, header in enumerate(headers):
        if not isinstance(header, str):
            continue
        if header in (*REQUIRED_COLUMNS, TIME_COLUMN):
            field = header
        elif header.strip().lower() in DESCRIPTIVE_COLUMNS:
            field = header.strip().lower()
        else:
            continue
        if field in mapping:
            notes.append(f"repeated {field} column ignored")
            continue
        mapping[field] = idx
    return mapping, notes


def score_columns(mapping: dict[str, int]) -> float:
    """Fraction of ethoscopy's required columns present (1.0 = usable)."""
    return round(sum(c in mapping for c in REQUIRED_COLUMNS) / len(REQUIRED_COLUMNS), 3)


def is_metadata(mapping: dict[str, int]) -> bool:
    """True if a header has every column ethoscopy needs."""
    return all(c in mapping for c in REQUIRED_COLUMNS)


def find_header(frame: pd.DataFrame) -> tuple[int | None, dict[str, int], list[str]]:
    """Map the header of a sheet read without a header.

    As in ethoscopy, the header is the first row; title rows are not skipped.

    Returns:
        ``(0, mapping, notes)``, or ``(None, {}, [])`` for an empty sheet.
    """
    if frame.empty:
        return None, {}, []
    mapping, notes = map_columns(list(frame.iloc[0]))
    return 0, mapping, notes


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def guess_delimiter(lines: list[str]) -> str:
    """Pick the delimiter appearing on the most lines (ties: most often).

    ``csv.Sniffer`` gives up on ragged lines, which are common in hand-made
    metadata, so a simple count is used instead.
    """
    best, best_key = ",", (0, 0)
    for delimiter in (",", ";", "\t", "|"):
        counts = [line.count(delimiter) for line in lines if line.strip()]
        key = (sum(c > 0 for c in counts), sum(counts))
        if key > best_key:
            best, best_key = delimiter, key
    return best


def _read_text(path: str) -> pd.DataFrame:
    with open(path, "rb") as handle:
        text = _decode(handle.read())
    lines = text.splitlines()
    if path.lower().endswith(".tsv"):
        delimiter = "\t"
    else:
        delimiter = guess_delimiter(lines[:50])
    rows = list(csv.reader(lines, delimiter=delimiter))
    width = max((len(r) for r in rows), default=0)
    return pd.DataFrame([r + [""] * (width - len(r)) for r in rows], dtype=object)


def read_tabular(path: str) -> list[tuple[str, pd.DataFrame]]:
    """Read every sheet of a tabular file, without interpreting a header.

    Args:
        path: A csv/tsv/txt/xlsx/xlsm/xls file.

    Returns:
        ``[(sheet_name, frame), ...]``; text files have a single sheet ``""``.
        Cells keep their native types (Excel dates stay datetimes).

    Raises:
        ValueError: For unsupported extensions.
        Exception: Whatever the underlying reader raises for corrupt files.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension in TEXT_EXTENSIONS:
        return [("", _read_text(path))]
    if extension in EXCEL_EXTENSIONS:
        engine = "xlrd" if extension == ".xls" else "openpyxl"
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object, engine=engine)
        return [(str(name), frame) for name, frame in sheets.items()]
    raise ValueError(f"unsupported file type: {extension}")


def sha256_file(path: str) -> str:
    """Return the hex SHA-256 of a file's content."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if is_blank(value):
        return None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _header_names(header_cells: list[Any]) -> list[str]:
    names, seen = [], {}
    for idx, cell in enumerate(header_cells):
        name = str(cell).strip() if not is_blank(cell) else f"column_{idx + 1}"
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name}.{seen[name] - 1}")
    return names


def build_rows(
    frame: pd.DataFrame, header_row: int, mapping: dict[str, int]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Turn a sheet into ``meta_row`` records.

    Args:
        frame: The sheet as read by :func:`read_tabular`.
        header_row: Index of the header row in ``frame``.
        mapping: Canonical field -> column index, from :func:`find_header`.

    Returns:
        ``(rows, info)``: row records and sheet-level facts (column names,
        date status counts).
    """
    names = _header_names(list(frame.iloc[header_row]))
    body = frame.iloc[header_row + 1 :]

    def cell(values: list[Any], field: str) -> Any:
        idx = mapping.get(field)
        return values[idx] if idx is not None and idx < len(values) else None

    rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for offset, values in enumerate(body.itertuples(index=False, name=None)):
        values = list(values)
        if all(is_blank(v) for v in values):
            continue
        raw = {name: _jsonable(v) for name, v in zip(names, values)}
        parsed = parse_date(cell(values, "date"))
        status_counts[parsed.status] = status_counts.get(parsed.status, 0) + 1
        used = set(mapping.values())
        other = {n: raw[n] for i, n in enumerate(names) if i not in used and raw[n] is not None}
        rows.append(
            {
                "row_idx": header_row + 1 + offset,
                "raw_json": json.dumps(raw, ensure_ascii=False),
                "date": parsed.date.isoformat() if parsed.date else None,
                "date_status": parsed.status,
                "start_time": parse_time(cell(values, TIME_COLUMN)),
                "machine_name": normalise_machine_name(cell(values, "machine_name")),
                "region_id": normalise_region_id(cell(values, "region_id")),
                "sex": normalise_sex(cell(values, "sex")),
                "genotype_raw": clean_text(cell(values, "genotype")),
                "treatment_raw": clean_text(cell(values, "treatment")),
                "other_json": json.dumps(other, ensure_ascii=False) if other else None,
            }
        )
    info = {"columns": names, "date_status": status_counts}
    return rows, info


def ingest_sheet(conn: sqlite3.Connection, meta_file_id: int, frame: pd.DataFrame) -> int:
    """Parse one registered sheet and replace its rows in the census.

    Args:
        conn: Census connection.
        meta_file_id: The ``meta_file`` row of this sheet.
        frame: The sheet content.

    Returns:
        Number of rows stored (0 when the header lacks a required column).
    """
    header_row, mapping, notes = find_header(frame)
    if header_row is None or not is_metadata(mapping):
        missing = [c for c in REQUIRED_COLUMNS if c not in mapping]
        note = f"missing column(s): {', '.join(missing)}"
        store.update_meta_file(conn, meta_file_id, status="no_key_columns", note=note)
        return 0
    rows, info = build_rows(frame, header_row, mapping)
    invalid = info["date_status"].get("invalid", 0)
    if invalid:
        notes.append(f"{invalid} date(s) not readable as dd/mm/yyyy or ISO")
    n_rows = store.replace_meta_rows(conn, meta_file_id, rows)
    store.update_meta_file(
        conn,
        meta_file_id,
        status="ingested" if rows else "empty",
        n_rows=n_rows,
        header_row=header_row,
        columns_raw=json.dumps(info["columns"], ensure_ascii=False),
        column_map=json.dumps({f: info["columns"][i] for f, i in mapping.items()}),
        note="; ".join(notes) or None,
    )
    return n_rows
