"""Census configuration: which trees hold databases and metadata, read from TOML.

Example ``census.toml``::

    timezone = "Europe/London"

    [[results]]
    path = "/mnt/data/results"

    [[results]]                      # a second copy: only the runs the first lacks
    path = "/mnt/backup/results"
    files_from = "/srv/census/backup_only.txt"

    [[metadata]]
    path = "/mnt/homes"
    tag = "homes"
    exclude = ["secrets", "cache"]

    [[metadata]]
    path = "/mnt/archive/alumni"
    tag = "alumni"

``files_from`` lists paths (one per line, relative to ``path``) to read instead
of walking the whole tree. Relative ``files_from`` paths are resolved against
the folder of the configuration file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from .match import DEFAULT_TIME_TOLERANCE_MIN, DEFAULT_TIMEZONE


@dataclass(frozen=True)
class ResultsRoot:
    """A tree of ethoscope databases."""

    path: str
    files_from: str | None = None


@dataclass(frozen=True)
class MetadataRoot:
    """A tree of metadata files, with the location tag shown in reports."""

    path: str
    tag: str
    exclude: tuple[str, ...] = ()
    files_from: str | None = None
    txt: bool = True


@dataclass(frozen=True)
class CensusConfig:
    """Everything ``ethocensus refresh`` needs besides the census file."""

    results: tuple[ResultsRoot, ...] = ()
    metadata: tuple[MetadataRoot, ...] = ()
    timezone: str = DEFAULT_TIMEZONE
    time_tolerance_min: float = DEFAULT_TIME_TOLERANCE_MIN
    source: str | None = field(default=None, compare=False)

    @property
    def tags(self) -> list[tuple[str, str]]:
        """``(root path, tag)`` pairs for :func:`summarise.summarise`."""
        return [(m.path, m.tag) for m in self.metadata]


class ConfigError(ValueError):
    """Raised for a configuration file that cannot be used."""


def _resolve(base: str, value: str | None) -> str | None:
    if value is None:
        return None
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(base, value))


def load_config(path: str) -> CensusConfig:
    """Read and validate a census TOML configuration.

    Args:
        path: The ``census.toml`` file.

    Returns:
        The configuration.

    Raises:
        ConfigError: For unknown keys or entries without a path (or tag).
    """
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    base = os.path.dirname(os.path.abspath(path))
    unknown = set(raw) - {"timezone", "time_tolerance_min", "results", "metadata"}
    if unknown:
        raise ConfigError(f"unknown key(s) in {path}: {', '.join(sorted(unknown))}")
    results, metadata = [], []
    for entry in raw.get("results", []):
        extra = set(entry) - {"path", "files_from"}
        if "path" not in entry or extra:
            raise ConfigError(f"[[results]] needs 'path' (and optional 'files_from'): {entry}")
        results.append(ResultsRoot(entry["path"], _resolve(base, entry.get("files_from"))))
    for entry in raw.get("metadata", []):
        extra = set(entry) - {"path", "tag", "exclude", "files_from", "txt"}
        if "path" not in entry or "tag" not in entry or extra:
            raise ConfigError(f"[[metadata]] needs 'path' and 'tag': {entry}")
        metadata.append(
            MetadataRoot(
                entry["path"],
                entry["tag"],
                tuple(entry.get("exclude", ())),
                _resolve(base, entry.get("files_from")),
                bool(entry.get("txt", True)),
            )
        )
    return CensusConfig(
        tuple(results),
        tuple(metadata),
        raw.get("timezone", DEFAULT_TIMEZONE),
        float(raw.get("time_tolerance_min", DEFAULT_TIME_TOLERANCE_MIN)),
        source=os.path.abspath(path),
    )


def read_path_list(path: str | None) -> list[str] | None:
    """Paths listed one per line (blank lines and ``#`` comments ignored).

    Bytes that are not UTF-8 (old file names) are kept as the file system
    gives them, so such files are found and then reported as ``bad_name``.

    Returns:
        The paths, or ``None`` when ``path`` is ``None`` (walk the whole tree).
    """
    if path is None:
        return None
    with open(path, encoding="utf-8", errors="surrogateescape") as handle:
        lines = (line.strip() for line in handle)
        return [line for line in lines if line and not line.startswith("#")]
