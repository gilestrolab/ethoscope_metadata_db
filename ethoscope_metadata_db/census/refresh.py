"""One incremental census refresh driven by a :class:`config.CensusConfig`.

Runs, in order: ``scan-dbs`` on every results root, ``discover`` on every
metadata root, ``ingest``, ``match`` and ``summarise``. Every step is
incremental except matching and summarising, which are cheap full
recomputations.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from . import discover as discover_mod
from .config import CensusConfig, read_path_list
from .dbindex import scan_results
from .match import match_all
from .summarise import summarise

log = logging.getLogger(__name__)


def refresh(
    conn: sqlite3.Connection,
    config: CensusConfig,
    progress: Callable[[str, Any], None] | None = None,
) -> dict[str, Any]:
    """Bring the census up to date with every configured root.

    Args:
        conn: Census connection.
        config: Roots and matching settings.
        progress: Called with ``(step, counts)`` after each step.

    Returns:
        Counts per step, keyed by a step label.
    """
    out: dict[str, Any] = {}

    def done(step: str, counts: Any) -> None:
        out[step] = counts
        log.info("%s: %s", step, counts)
        if progress:
            progress(step, counts)

    for root in config.results:
        counts = scan_results(conn, root.path, paths=read_path_list(root.files_from))
        done(f"scan-dbs {root.path}", counts)
    for root in config.metadata:
        extensions = set(discover_mod.DEFAULT_EXTENSIONS)
        if not root.txt:
            extensions.discard(".txt")
        counts = discover_mod.discover(
            conn,
            root.path,
            extensions,
            discover_mod.DEFAULT_MAX_MB,
            root.exclude,
            read_path_list(root.files_from),
        )
        done(f"discover {root.tag} {root.path}", counts)
    done("ingest", discover_mod.ingest_candidates(conn))
    done("match", match_all(conn, config.time_tolerance_min, config.timezone))
    done("summarise", summarise(conn, config.tags, config.timezone))
    return out
