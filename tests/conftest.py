"""Synthetic ethoscope databases and messy metadata files for the census tests."""

from __future__ import annotations

import calendar
import datetime as dt
import os
import sqlite3
from typing import Any

import pytest

from ethoscope_metadata_db.census import store

PICAM2_HW = {"kernel": "6.1.0-rpi7-rpi-v8", "pi_version": "4", "camera": {"sensor": "imx708"}}
LEGACY_HW = {"kernel": "4.19.66-1-ARCH", "pi_version": "3", "camera": "imx219"}
STIM_OPTIONS = (
    "{'interactor': {'name': \"<class 'ethoscope.stimulators.sleep_depriver_stimulators"
    ".DefaultStimulator'>\", 'kwargs': {'velocity_threshold': 0.006}}, "
    "'tracker': {'name': \"<class 'ethoscope.trackers.adaptive_bg_tracker.AdaptiveBGModel'>\","
    " 'kwargs': {}}}"
)
PLAIN_OPTIONS = repr(
    {
        "interactor": {"name": "DefaultInteractor", "kwargs": {}},
        "tracker": {"name": "AdaptiveBGModel", "kwargs": {}},
    }
)


def machine_id_for(number: int) -> str:
    """A 32-hex machine id that encodes the machine number."""
    return f"{number:03d}" + "c" * 29


def make_ethoscope_db(
    path: str,
    *,
    machine_number: int,
    start: dt.datetime,
    n_rois: int = 20,
    roi_ids: list[int] | None = None,
    stop_after_h: float | None = None,
    last_t_h: float | None = None,
    hardware_info: Any = PICAM2_HW,
    selected_options: str = PLAIN_OPTIONS,
    is_inferred_text: bool = False,
    experimental_info: Any = None,
) -> str:
    """Write a small ethoscope-like SQLite database.

    Args:
        path: Where to write it (parent directories are created).
        machine_number: Number of the ETHOSCOPE_NNN machine.
        start: Start time, interpreted as UTC.
        n_rois: Number of ROIs in ROI_MAP (ids 1..n) unless ``roi_ids`` given.
        roi_ids: Explicit ROI ids.
        stop_after_h: If set, write ``stop_date_time`` this many hours later.
        last_t_h: If set, write tracking rows up to this many hours (``t`` in ms).
        hardware_info: Dict (stored as its repr) or plain string.
        selected_options: Stored verbatim.
        is_inferred_text: Store ``is_inferred`` as text '0'/'1' (a firmware quirk).
        experimental_info: Dict stored as its repr in METADATA (default: user
            ``alice`` in ``Incubator_1``).

    Returns:
        ``path``.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    epoch = calendar.timegm(start.timetuple())
    meta = {
        "machine_id": machine_id_for(machine_number),
        "machine_name": f"ETHOSCOPE_{machine_number:03d}",
        "date_time": str(epoch),
        "version": "a1b2c3",
        "hardware_info": repr(hardware_info) if isinstance(hardware_info, dict) else hardware_info,
        "selected_options": selected_options,
        "gain_setting": "1.5",
        "maxfps_setting": "15",
        "experimental_info": repr(
            experimental_info
            if experimental_info is not None
            else {"name": "alice", "location": "Incubator_1", "code": ""}
        ),
    }
    if stop_after_h is not None:
        meta["stop_date_time"] = str(epoch + int(stop_after_h * 3600))
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE METADATA (field TEXT, value TEXT)")
    conn.executemany("INSERT INTO METADATA VALUES (?, ?)", meta.items())
    conn.execute(
        "CREATE TABLE ROI_MAP (roi_idx SMALLINT, roi_value SMALLINT, x SMALLINT, "
        "y SMALLINT, w SMALLINT, h SMALLINT)"
    )
    ids = roi_ids if roi_ids is not None else list(range(1, n_rois + 1))
    conn.executemany("INSERT INTO ROI_MAP VALUES (?, ?, 0, 0, 10, 10)", [(i, i) for i in ids])
    inferred_type = "TEXT" if is_inferred_text else "TINYINT"
    for roi in ids:
        conn.execute(
            f"CREATE TABLE ROI_{roi} (id INTEGER PRIMARY KEY, t INTEGER, x REAL, y REAL, "
            f"is_inferred {inferred_type})"
        )
        if last_t_h is not None:
            flag = "0" if is_inferred_text else 0
            rows = [(int(last_t_h * 3.6e6 * k / 4), 1.0, 1.0, flag) for k in range(5)]
            conn.executemany(
                f"INSERT INTO ROI_{roi} (t, x, y, is_inferred) VALUES (?, ?, ?, ?)", rows
            )
    conn.commit()
    conn.close()
    return path


def tree_db_path(root: str, machine_number: int, start: dt.datetime) -> str:
    """Path of a database in the standard results-tree layout."""
    mid = machine_id_for(machine_number)
    stamp = start.strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(root, mid, f"ETHOSCOPE_{machine_number:03d}", stamp, f"{stamp}_{mid}.db")


@pytest.fixture
def results_root(tmp_path) -> str:
    """A results tree with a mix of normal, ambiguous and broken databases.

    ETHOSCOPE_007: 2024-01-05 10:00 (3-day run, stimulator, picamera2),
                   2024-01-10 09:00 and 2024-01-10 15:00 (two same-day runs).
    ETHOSCOPE_012: 2024-03-25 10:00 (10 ROIs, legacy, no stop_date_time),
                   2024-04-01 10:00 with a non-empty -wal sidecar.
    ETHOSCOPE_020: a corrupt file.
    """
    root = str(tmp_path / "results")
    make_ethoscope_db(
        tree_db_path(root, 7, dt.datetime(2024, 1, 5, 10)),
        machine_number=7,
        start=dt.datetime(2024, 1, 5, 10),
        stop_after_h=72,
        selected_options=STIM_OPTIONS,
    )
    for hour in (9, 15):
        start = dt.datetime(2024, 1, 10, hour)
        make_ethoscope_db(
            tree_db_path(root, 7, start), machine_number=7, start=start, stop_after_h=5
        )
    start = dt.datetime(2024, 3, 25, 10)
    make_ethoscope_db(
        tree_db_path(root, 12, start),
        machine_number=12,
        start=start,
        n_rois=10,
        last_t_h=48,
        hardware_info=LEGACY_HW,
        is_inferred_text=True,
    )
    start = dt.datetime(2024, 4, 1, 10)
    wal_db = make_ethoscope_db(tree_db_path(root, 12, start), machine_number=12, start=start)
    with open(wal_db + "-wal", "wb") as handle:
        handle.write(b"\x00" * 64)
    corrupt = tree_db_path(root, 20, dt.datetime(2024, 5, 1, 10))
    os.makedirs(os.path.dirname(corrupt))
    with open(corrupt, "wb") as handle:
        handle.write(b"this is not a database" * 100)
    return root


def write_text(path: str, text: str, encoding: str = "utf-8") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding, newline="") as handle:
        handle.write(text)
    return path


@pytest.fixture
def homes_root(tmp_path) -> str:
    """User homes with metadata files, usable or not by ethoscopy's rules.

    alice/exp1/metadata.csv: ethoscopy columns, but semicolon-separated,
        cp1252, dd/mm/yyyy dates.
    alice/exp0/titled.csv: right columns below a title row: rejected.
    alice/notes/aliased.csv: Date/Ethoscope/ROI headers: rejected.
    alice/notes/shopping.csv: not metadata.
    alice/.ipynb_checkpoints/metadata-checkpoint.csv: hidden, must be skipped.
    bob/flies.xlsx: sheet 'flies' with real date cells and numeric machine
        names; sheet 'readme' with prose.
    carol/timed.tsv: a dd/mm/yyyy row among ISO dates, and a ``time`` column.
    """
    import pandas as pd

    root = str(tmp_path / "homes")
    write_text(
        os.path.join(root, "alice", "exp1", "metadata.csv"),
        "date;machine_name;region_id;Genotype;Sex;Notes\n"
        "05/01/2024;ETHOSCOPE_007;1;CS;M;\n"
        "05/01/2024;ETHOSCOPE_7;2; CS ;male;dead day 2 – é\n"
        "05/01/2024;7;25;w1118;F;\n"
        "25/03/2024;12;3;Dh31>Kir;f;\n"
        "25/03/2024;12;15;Dh31>Kir;f;\n"
        ";;;;;\n"
        "07/01/2024;7;4;CS;M;\n"
        "02/02/2024;99;1;CS;M;\n"
        ";007;5;CS;M;\n",
        encoding="cp1252",
    )
    write_text(
        os.path.join(root, "alice", "exp0", "titled.csv"),
        "Sleep deprivation, January,\ndate,machine_name,region_id\n2024-01-05,ETHOSCOPE_007,1\n",
    )
    write_text(
        os.path.join(root, "alice", "notes", "aliased.csv"),
        "Date,Ethoscope,ROI\n2024-01-05,ETHOSCOPE_007,1\n",
    )
    write_text(
        os.path.join(root, "alice", "notes", "shopping.csv"),
        "item,quantity\nagar,2\nyeast,5\n",
    )
    write_text(
        os.path.join(root, "alice", ".ipynb_checkpoints", "metadata-checkpoint.csv"),
        "date,machine_name,region_id\n2024-01-05,ETHOSCOPE_007,1\n",
    )
    flies = pd.DataFrame(
        {
            "machine_name": [7, 7, 7],
            "region_id": [1.0, 3.0, 4.0],
            "date": [dt.datetime(2024, 1, 5), dt.datetime(2024, 1, 5), dt.datetime(2024, 1, 10)],
            "genotype": ["CS", "CS", "CS"],
            "treatment": ["SD", "SD", None],
        }
    )
    xlsx = os.path.join(root, "bob", "flies.xlsx")
    os.makedirs(os.path.dirname(xlsx))
    with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
        flies.to_excel(writer, sheet_name="flies", index=False)
        pd.DataFrame({"About": ["Some notes about the experiment"]}).to_excel(
            writer, sheet_name="readme", index=False
        )
    write_text(
        os.path.join(root, "carol", "timed.tsv"),
        "date\tmachine_name\tregion_id\tgenotype\ttime\n"
        "10/01/2024\tETHOSCOPE_007\t1\tCS\t\n"
        "2024-01-10\tETHOSCOPE_007\t2\tCS\t15-00-00\n"
        "2024-01-10\tETHOSCOPE_007\t3\tCS\t\n"
        "2024-01-10\tETHOSCOPE_007\t4\tCS\t12:00\n"
        "2024-01-10\tETHOSCOPE_007\t5\tCS\t14:30\n",
    )
    return root


@pytest.fixture
def census(tmp_path):
    """An empty census connection."""
    conn = store.connect(tmp_path / "census.sqlite")
    yield conn
    conn.close()
