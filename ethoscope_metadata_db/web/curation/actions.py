"""The edits people make, each one logged and undoable (see :class:`store.Change`)."""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable

from .store import Change, CurationError, now

LABEL_KINDS = ("tag", "project")
ALIAS_KINDS = ("experimenter", "home", "other")
TARGET_TYPES = ("run", "file", "folder")
MAX_NAME = 120


def clean_name(name: str) -> str:
    """Trim and collapse spaces; refuse empty or overlong names."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name:
        raise CurationError("a name is required")
    if len(name) > MAX_NAME:
        raise CurationError(f"names are at most {MAX_NAME} characters")
    return name


def _label(conn: sqlite3.Connection, label_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM label WHERE id = ?", (label_id,)).fetchone()
    if row is None:
        raise CurationError("no such label")
    return row


def _person(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise CurationError("no such person")
    return row


# ---- labels -------------------------------------------------------------------------------


def create_label(
    conn: sqlite3.Connection,
    who: str | None,
    name: str,
    kind: str = "tag",
    doi: str = "",
    description: str = "",
    authors: str = "",
) -> int:
    """Create a label (a free tag, or a project with optional DOI/description/authors)."""
    name = clean_name(name)
    if kind not in LABEL_KINDS:
        raise CurationError(f"kind must be one of {', '.join(LABEL_KINDS)}")
    if conn.execute("SELECT 1 FROM label WHERE name = ?", (name,)).fetchone():
        raise CurationError(f"a label called {name!r} already exists")
    with Change(conn, who, "create label", f"created label {name!r}") as change:
        row = change.insert(
            "label",
            {
                "name": name,
                "kind": kind,
                "doi": doi.strip() or None,
                "description": description.strip() or None,
                "authors": authors.strip() or None,
            },
        )
    return int(row["id"])


def edit_label(conn: sqlite3.Connection, who: str | None, label_id: int, **fields: str) -> None:
    """Rename a label or change its kind, DOI, description or authors."""
    old = _label(conn, label_id)
    values = {}
    for key, value in fields.items():
        if key not in ("name", "kind", "doi", "description", "authors"):
            continue
        if key == "name":
            value = clean_name(value)
            clash = conn.execute(
                "SELECT id FROM label WHERE name = ? AND id != ?", (value, label_id)
            ).fetchone()
            if clash:
                raise CurationError(f"a label called {value!r} already exists: merge them instead")
        elif key == "kind" and value not in LABEL_KINDS:
            raise CurationError(f"kind must be one of {', '.join(LABEL_KINDS)}")
        else:
            value = (value or "").strip() or None
        values[key] = value
    with Change(conn, who, "edit label", f"edited label {old['name']!r}") as change:
        change.update("label", {"id": label_id}, values)


def link(
    conn: sqlite3.Connection,
    who: str | None,
    label_id: int,
    target_type: str,
    targets: Iterable[str],
) -> int:
    """Attach a label to runs, files or folders (by path). Returns how many were new."""
    label = _label(conn, label_id)
    if target_type not in TARGET_TYPES:
        raise CurationError(f"target must be one of {', '.join(TARGET_TYPES)}")
    targets = sorted({t.rstrip("/") if target_type == "folder" else t for t in targets if t})
    if not targets:
        raise CurationError("nothing selected")
    with Change(
        conn, who, "label", f"labelled {len(targets)} {target_type}(s) {label['name']!r}"
    ) as change:
        for target in targets:
            change.insert(
                "label_link", {"label_id": label_id, "target_type": target_type, "target": target}
            )
        added = len(change.ops)
    return added


def unlink(
    conn: sqlite3.Connection,
    who: str | None,
    label_id: int,
    target_type: str,
    targets: Iterable[str],
) -> None:
    label = _label(conn, label_id)
    targets = list(targets)
    with Change(
        conn, who, "unlabel", f"removed {label['name']!r} from {len(targets)} {target_type}(s)"
    ) as change:
        for target in targets:
            change.delete(
                "label_link", {"label_id": label_id, "target_type": target_type, "target": target}
            )


def merge_labels(conn: sqlite3.Connection, who: str | None, source_id: int, target_id: int) -> None:
    """Move every link of ``source`` to ``target`` and delete ``source``."""
    if source_id == target_id:
        raise CurationError("cannot merge a label into itself")
    source, target = _label(conn, source_id), _label(conn, target_id)
    links = conn.execute(
        "SELECT target_type, target FROM label_link WHERE label_id = ?", (source_id,)
    ).fetchall()
    with Change(
        conn, who, "merge labels", f"merged {source['name']!r} into {target['name']!r}"
    ) as change:
        for row in links:
            change.insert(
                "label_link",
                {"label_id": target_id, "target_type": row["target_type"], "target": row["target"]},
            )
            change.delete(
                "label_link",
                {"label_id": source_id, "target_type": row["target_type"], "target": row["target"]},
            )
        change.delete("label", {"id": source_id})


def delete_label(conn: sqlite3.Connection, who: str | None, label_id: int) -> None:
    label = _label(conn, label_id)
    links = conn.execute(
        "SELECT target_type, target FROM label_link WHERE label_id = ?", (label_id,)
    ).fetchall()
    with Change(
        conn, who, "delete label", f"deleted label {label['name']!r} ({len(links)} links)"
    ) as change:
        for row in links:
            change.delete(
                "label_link",
                {"label_id": label_id, "target_type": row["target_type"], "target": row["target"]},
            )
        change.delete("label", {"id": label_id})


# ---- people -------------------------------------------------------------------------------


def create_person(
    conn: sqlite3.Connection, who: str | None, name: str, aliases: Iterable[tuple[str, str]] = ()
) -> int:
    """Create a person, optionally taking over some aliases (moved from whoever had them)."""
    name = clean_name(name)
    if conn.execute("SELECT 1 FROM person WHERE name = ?", (name,)).fetchone():
        raise CurationError(f"a person called {name!r} already exists: add aliases to them instead")
    aliases = list(aliases)
    with Change(
        conn, who, "create person", f"created person {name!r} ({len(aliases)} alias(es))"
    ) as change:
        person_id = int(change.insert("person", {"name": name})["id"])
        for kind, value in aliases:
            _set_alias(conn, change, person_id, kind, value)
    return person_id


def _set_alias(
    conn: sqlite3.Connection, change: Change, person_id: int, kind: str, value: str
) -> None:
    if kind not in ALIAS_KINDS:
        raise CurationError(f"alias kind must be one of {', '.join(ALIAS_KINDS)}")
    value = (value or "").strip()
    if not value:
        raise CurationError("an alias needs a value")
    existing = conn.execute(
        "SELECT person_id FROM person_alias WHERE kind = ? AND value = ?", (kind, value)
    ).fetchone()
    if existing is None:
        change.insert("person_alias", {"kind": kind, "value": value, "person_id": person_id})
    elif existing["person_id"] != person_id:
        change.update("person_alias", {"kind": kind, "value": value}, {"person_id": person_id})


def add_aliases(
    conn: sqlite3.Connection, who: str | None, person_id: int, aliases: Iterable[tuple[str, str]]
) -> None:
    person = _person(conn, person_id)
    aliases = list(aliases)
    with Change(
        conn, who, "add alias", f"gave {person['name']!r} {len(aliases)} alias(es)"
    ) as change:
        for kind, value in aliases:
            _set_alias(conn, change, person_id, kind, value)


def remove_alias(conn: sqlite3.Connection, who: str | None, kind: str, value: str) -> None:
    with Change(conn, who, "remove alias", f"removed alias {kind}:{value!r}") as change:
        change.delete("person_alias", {"kind": kind, "value": value})


def merge_people(conn: sqlite3.Connection, who: str | None, source_id: int, target_id: int) -> None:
    """Move every alias of ``source`` to ``target`` and delete ``source``."""
    if source_id == target_id:
        raise CurationError("cannot merge a person into themselves")
    source, target = _person(conn, source_id), _person(conn, target_id)
    aliases = conn.execute(
        "SELECT kind, value FROM person_alias WHERE person_id = ?", (source_id,)
    ).fetchall()
    with Change(
        conn, who, "merge people", f"merged {source['name']!r} into {target['name']!r}"
    ) as change:
        for row in aliases:
            change.update(
                "person_alias",
                {"kind": row["kind"], "value": row["value"]},
                {"person_id": target_id},
            )
        change.delete("person", {"id": source_id})


def rename_person(conn: sqlite3.Connection, who: str | None, person_id: int, name: str) -> None:
    person = _person(conn, person_id)
    name = clean_name(name)
    if conn.execute(
        "SELECT 1 FROM person WHERE name = ? AND id != ?", (name, person_id)
    ).fetchone():
        raise CurationError(f"a person called {name!r} already exists: merge them instead")
    with Change(conn, who, "rename person", f"renamed {person['name']!r} to {name!r}") as change:
        change.update("person", {"id": person_id}, {"name": name})


def delete_person(conn: sqlite3.Connection, who: str | None, person_id: int) -> None:
    person = _person(conn, person_id)
    aliases = conn.execute(
        "SELECT kind, value FROM person_alias WHERE person_id = ?", (person_id,)
    ).fetchall()
    with Change(conn, who, "delete person", f"deleted person {person['name']!r}") as change:
        for row in aliases:
            change.delete("person_alias", {"kind": row["kind"], "value": row["value"]})
        change.delete("person", {"id": person_id})


# ---- notes --------------------------------------------------------------------------------


def add_note(
    conn: sqlite3.Connection, who: str | None, target_type: str, target: str, text: str
) -> None:
    text = (text or "").strip()
    if not text:
        raise CurationError("the note is empty")
    if len(text) > 5000:
        raise CurationError("notes are at most 5000 characters")
    with Change(conn, who, "note", f"note on {target_type} {target}") as change:
        change.insert(
            "note",
            {"target_type": target_type, "target": target, "text": text, "at": now(), "who": who},
        )


def delete_note(conn: sqlite3.Connection, who: str | None, note_id: int) -> None:
    with Change(conn, who, "delete note", f"deleted note {note_id}") as change:
        change.delete("note", {"id": note_id})
