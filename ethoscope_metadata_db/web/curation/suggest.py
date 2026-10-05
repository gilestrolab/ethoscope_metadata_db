"""Suggestions for curation: aliases that probably name one person, folders that look like projects.

Nothing here changes anything; the UI offers each suggestion for a person to
accept with one click.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Any

METADATA_OUTCOMES = ("all matched", "some unmatched", "none matched", "empty")


def alias_key(value: str) -> str:
    """Case, spacing and punctuation-free form: ``Ann_and Bob`` -> ``annandbob``."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def initial_key(value: str) -> str | None:
    """``grace.hopper`` -> ``ghopper``, the lab's home-folder naming (first initial + surname)."""
    parts = [p for p in re.split(r"[._\s-]+", value.lower()) if p]
    if len(parts) < 2 or not parts[0][:1].isalpha():
        return None
    return parts[0][0] + "".join(parts[1:])


def alias_groups(conn: sqlite3.Connection, limit: int = 1000) -> list[dict[str, Any]]:
    """Experimenter strings and home names grouped when they probably name one person.

    A group joins values with the same :func:`alias_key`, and a ``first.last``
    experimenter with the ``flast`` home. Only groups with at least one value
    not yet assigned to a person are returned, biggest first.

    Args:
        conn: Census connection with the curation file attached as ``cur``.
    """
    assigned = {
        (r["kind"], r["value"]): (r["name"], r["person_kind"])
        for r in conn.execute(
            "SELECT a.kind, a.value, p.name, p.kind AS person_kind FROM cur.person_alias a "
            "JOIN cur.person p ON p.id = a.person_id"
        )
    }
    items: list[dict[str, Any]] = []
    for value, n in conn.execute(
        "SELECT experimenter, count(*) FROM db_summary WHERE experimenter IS NOT NULL "
        "AND experimenter != '' GROUP BY 1"
    ):
        items.append({"kind": "experimenter", "value": value, "n": n, "unit": "runs"})
    for value, n in conn.execute(
        "SELECT home, count(*) FROM file_summary WHERE home IS NOT NULL AND home != '' "
        f"AND outcome IN ({', '.join('?' * len(METADATA_OUTCOMES))}) GROUP BY 1",
        METADATA_OUTCOMES,
    ):
        items.append({"kind": "home", "value": value, "n": n, "unit": "metadata files"})

    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_key: dict[str, int] = {}
    for i, item in enumerate(items):
        for key in filter(None, (alias_key(item["value"]), initial_key(item["value"]))):
            if key in by_key:
                parent[find(i)] = find(by_key[key])
            else:
                by_key[key] = i
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for i, item in enumerate(items):
        item["person"], item["person_kind"] = assigned.get(
            (item["kind"], item["value"]), (None, None)
        )
        groups[find(i)].append(item)
    out = []
    for members in groups.values():
        if all(m["person"] for m in members):
            continue
        members.sort(key=lambda m: -m["n"])
        # Reason: suggest joining a real person only; a name already marked
        # "not a person" says nothing about who the rest of the group is.
        people = sorted({m["person"] for m in members if m["person_kind"] == "person"})
        out.append(
            {
                "members": members,
                "people": people,
                "suggested": people[0] if people else _display_name(members),
                "total": sum(m["n"] for m in members),
            }
        )
    out.sort(key=lambda g: (len(g["members"]) == 1, -g["total"]))  # real groups first
    return out[:limit]


def _display_name(members: list[dict[str, Any]]) -> str:
    """Prefer a dotted ``first.last`` experimenter as the person's name, else the commonest."""
    dotted = [m["value"] for m in members if m["kind"] == "experimenter" and "." in m["value"]]
    return dotted[0] if dotted else members[0]["value"]


def folder_suggestions(
    conn: sqlite3.Connection, min_files: int = 3, limit: int = 200
) -> list[dict[str, Any]]:
    """Folders under the homes that gather several metadata files: likely projects.

    A folder that only wraps a single subfolder holding the same files is left
    out in favour of that subfolder; folders already labelled are skipped.

    Args:
        conn: Census connection with the curation file attached as ``cur``.
    """
    labelled = {
        r[0] for r in conn.execute("SELECT target FROM cur.label_link WHERE target_type = 'folder'")
    }
    files: dict[str, set[str]] = defaultdict(set)
    where: dict[str, tuple[str, str]] = {}
    for path, home, tag in conn.execute(
        "SELECT path, home, tag FROM file_summary "
        f"WHERE outcome IN ({', '.join('?' * len(METADATA_OUTCOMES))})",
        METADATA_OUTCOMES,
    ):
        parts = path.split("/")
        try:
            start = parts.index(home) + 1 if home else len(parts) - 1
        except ValueError:
            continue
        for depth in range(start + 1, len(parts)):
            folder = "/".join(parts[:depth])
            files[folder].add(path)
            where.setdefault(folder, (home or "", tag or ""))
    children: dict[str, list[str]] = defaultdict(list)
    for folder in files:
        children[folder.rsplit("/", 1)[0]].append(folder)
    out = []
    for folder, members in files.items():
        if len(members) < min_files or folder in labelled:
            continue
        kids = children.get(folder, [])
        if len(kids) == 1 and files[kids[0]] == members:
            continue  # only a wrapper around one subfolder with the same files
        home, tag = where[folder]
        out.append(
            {
                "folder": folder,
                "name": folder.rsplit("/", 1)[-1],
                "n_files": len(members),
                "home": home,
                "tag": tag,
                "relative": folder.split(f"/{home}/", 1)[-1] if home else folder,
            }
        )
    out.sort(key=lambda s: (-s["n_files"], s["folder"]))
    return out[:limit]
