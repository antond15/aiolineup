"""Backup of the admin's own work as one JSON file: export, check and apply.

The file holds sources, selections, own categories, names and logos, stream choices, guide
mappings and settings. It holds no cache, stats, feed token, password or session secret.
Source URLs are exported exactly (they may carry a login), so the file is private.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO

from . import __version__, rules
from .db import DEFAULT_SETTINGS, Database
from .engine import MAX_CATEGORIES, Engine

FORMAT = "aiolineup-backup"
VERSION = 1
MAX_BYTES = 12 * 1024 * 1024

# Section -> (table, columns, order). The columns are the names in the file.
_SECTIONS = {
    "sources": (
        "sources",
        ("id", "kind", "name", "url", "enabled", "priority", "created_at"),
        "id",
    ),
    "selections": (
        "selections",
        ("channel_key", "name", "added_at", "number"),
        "added_at, channel_key",
    ),
    "categories": (
        "category_overrides",
        ("channel_key", "position", "category"),
        "channel_key, position",
    ),
    "channel_edits": ("channel_edits", ("channel_key", "name", "logo"), "channel_key"),
    "stream_prefs": (
        "stream_prefs",
        ("channel_key", "signature", "position", "enabled"),
        "channel_key, position, signature",
    ),
    "epg_mappings": ("epg_mappings", ("channel_key", "epg_id", "source_id"), "channel_key"),
}
_BOOL_COLUMNS = {"enabled"}
# Derived data a backup never holds and an import clears (the next refresh rebuilds it).
_DERIVED_KV = ("playlist_stats", "epg_stats", "epg_coverage", "schedule_scanned", "metadata_state")
_USER_TABLES = ("selections", "category_overrides", "channel_edits", "stream_prefs", "epg_mappings")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_MAX_INT = 2**63 - 1  # what SQLite stores
# Text from a playlist has no length the app enforces, so only the 12 MiB file cap bounds it.
_LONG = 1_000_000


class BackupError(ValueError):
    """The file is not a backup this version can import; the message names the problem."""


@dataclass
class Backup:
    exported_at: str
    app_version: str
    # Rows are tuples in the column order of _SECTIONS (a dict per row costs far more memory).
    sources: list[tuple]
    selections: list[tuple]
    categories: list[tuple]
    channel_edits: list[tuple]
    stream_prefs: list[tuple]
    epg_mappings: list[tuple]
    settings: dict[str, Any]

    def summary(self) -> dict:
        return {
            "exported_at": self.exported_at,
            "app_version": self.app_version,
            "counts": {
                "sources": len(self.sources),
                "channels": len(self.selections),
                "guide_mappings": len(self.epg_mappings),
                "settings": len(self.settings),
            },
        }


# ---------------------------------------------------------------- export


def export(db: Database, fh: BinaryIO) -> None:
    """Writes the backup to `fh`, row by row, from one read transaction (so the sections
    agree with each other while the worker writes)."""
    conn = db.conn()
    conn.execute("BEGIN")
    try:
        head = {
            "format": FORMAT,
            "version": VERSION,
            "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "app_version": __version__,
        }
        fh.write(json.dumps(head)[:-1].encode())
        for name, (table, columns, order) in _SECTIONS.items():
            fh.write(f',\n"{name}": ['.encode())
            sql = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}"
            for i, row in enumerate(conn.execute(sql)):
                item = {
                    col: bool(value) if col in _BOOL_COLUMNS else value
                    for col, value in zip(columns, row, strict=True)
                }
                fh.write(
                    (",\n  " if i else "\n  ").encode()
                    + json.dumps(item, separators=(",", ":")).encode()
                )
            fh.write(b"\n]")
        fh.write(f',\n"settings": {json.dumps(db.settings())}\n}}\n'.encode())
    finally:
        conn.execute("ROLLBACK")  # a read transaction: nothing to commit


# ---------------------------------------------------------------- check


def _text(low: int, high: int):
    def check(value: Any) -> str:
        if not isinstance(value, str) or not low <= len(value) <= high:
            raise ValueError(f"expected text of {low} to {high} characters")
        if _SURROGATE_RE.search(value):
            raise ValueError("contains a character that cannot be stored")
        return value

    return check


def _int(low: int, high: int = _MAX_INT):
    def check(value: Any) -> int:
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"expected a whole number from {low} to {high}")
        return value

    return check


def _time(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 1e15:
        raise ValueError("expected a time (a number of seconds, 0 or more)")
    return value


def _bool(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("expected true or false")
    return value


def _opt(check):
    return lambda value: None if value is None else check(value)


def _source_field(field: str):
    text = _text(0, 5000)

    def check(value: Any) -> Any:
        return rules.source_fields({field: text(value)}, True)[field]

    return check


def _category(value: Any) -> str:
    return rules.categories([_text(0, 200)(value)])[0]


def _own_text(rule):
    """An own name or logo: null or "" = the source value (stored as null)."""

    def check(value: Any) -> str | None:
        return rule(_opt(_text(0, 2048))(value)) or None

    return check


def _guide_id(value: Any) -> str:
    return rules.epg_id(_text(0, 1000)(value))


_SPECS: dict[str, dict] = {
    "sources": {
        "id": _int(1),
        "kind": _source_field("kind"),
        "name": _source_field("name"),
        "url": _source_field("url"),
        "enabled": _bool,
        "priority": _int(0, 10000),
        "created_at": _time,
    },
    "selections": {
        "channel_key": _text(1, _LONG),
        "name": _text(0, _LONG),
        "added_at": _time,
        "number": _int(1),
    },
    "categories": {
        "channel_key": _text(1, _LONG),
        "position": _int(1, MAX_CATEGORIES),
        "category": _category,
    },
    "channel_edits": {
        "channel_key": _text(1, _LONG),
        "name": _own_text(rules.edit_name),
        "logo": _own_text(lambda v: rules.edit_logo(v, https_only=False)),
    },
    "stream_prefs": {
        "channel_key": _text(1, _LONG),
        "signature": _text(1, _LONG),
        "position": _int(0),
        "enabled": _bool,
    },
    "epg_mappings": {
        "channel_key": _text(1, _LONG),
        "epg_id": _guide_id,
        "source_id": _opt(_int(1)),
    },
}


def _rows(doc: dict, name: str) -> list[tuple]:
    """Checks one section and returns its rows as tuples; the section is dropped from `doc`
    as it is read, so the parsed text and the checked rows are never both held in full."""
    rows = doc[name]
    if not isinstance(rows, list):
        raise BackupError(f"{name} must be a list.")
    doc[name] = None
    spec = _SPECS[name]
    out = []
    for i in range(len(rows)):
        row, rows[i] = rows[i], None
        where = f"{name}[{i}]"
        if not isinstance(row, dict):
            raise BackupError(f"{where}: expected an object.")
        if unknown := sorted(set(row) - set(spec)):
            raise BackupError(f"{where}: unknown field {unknown[0]}.")
        if missing := sorted(set(spec) - set(row)):
            raise BackupError(f"{where}: missing field {missing[0]}.")
        clean = []
        for col, check in spec.items():
            try:
                clean.append(check(row[col]))
            except ValueError as exc:
                raise BackupError(f"{where}.{col}: {str(exc).rstrip('.')}.") from None
        out.append(tuple(clean))
    return out


def _unique(name: str, rows: list[tuple], *cols: str) -> None:
    at = [list(_SPECS[name]).index(c) for c in cols]
    seen = set()
    for i, row in enumerate(rows):
        key = tuple(row[j] for j in at)
        if key in seen:
            raise BackupError(f"{name}[{i}].{cols[-1]}: appears twice.")
        seen.add(key)


def check(doc: dict) -> Backup:
    """Validates a parsed backup with the API's own rules and returns it; any problem
    refuses the whole file (BackupError). Writes nothing."""
    if doc.get("format") != FORMAT:
        raise BackupError("This is not an AIOLineup backup file.")
    version = doc.get("version")
    if type(version) is not int or version < 1:
        raise BackupError("version must be a whole number from 1.")
    if version > VERSION:
        raise BackupError(
            f"This backup was made by a newer AIOLineup (format version {version}); "
            "update before importing it."
        )
    expected = {"format", "version", "exported_at", "app_version", "settings", *_SPECS}
    if unknown := sorted(set(doc) - expected):
        raise BackupError(f"Unknown key {unknown[0]}.")
    if missing := sorted(expected - set(doc)):
        raise BackupError(f"Missing {missing[0]}.")
    exported_at, app_version = doc["exported_at"], doc["app_version"]
    try:
        datetime.fromisoformat(_text(1, 40)(exported_at).replace("Z", "+00:00"))
        _text(0, 50)(app_version)
    except ValueError:
        raise BackupError("exported_at or app_version is not valid text.") from None

    sources = _rows(doc, "sources")
    selections = _rows(doc, "selections")
    categories = _rows(doc, "categories")
    channel_edits = _rows(doc, "channel_edits")
    stream_prefs = _rows(doc, "stream_prefs")
    epg_mappings = _rows(doc, "epg_mappings")
    _unique("sources", sources, "id")
    _unique("selections", selections, "channel_key")
    _unique("selections", selections, "number")
    _unique("categories", categories, "channel_key", "position")
    _unique("channel_edits", channel_edits, "channel_key")
    _unique("stream_prefs", stream_prefs, "channel_key", "signature")
    _unique("epg_mappings", epg_mappings, "channel_key")

    selected = {r[0] for r in selections}
    for i, row in enumerate(stream_prefs):
        if row[0] not in selected:
            raise BackupError(f"stream_prefs[{i}].channel_key: not a selected channel.")
    guides = {r[0] for r in sources if r[1] == "xmltv"}
    for i, row in enumerate(epg_mappings):
        source_id = row[2]
        if source_id is not None and (source_id not in guides or not row[1]):
            raise BackupError(f"epg_mappings[{i}].source_id: not a guide source of this file.")
    # The API refuses two categories of a channel that differ only in case.
    spelled: dict[str, set[str]] = {}
    for i, row in enumerate(categories):
        folded = spelled.setdefault(row[0], set())
        if row[2].casefold() in folded:
            raise BackupError(f"categories[{i}].category: this channel already has that category.")
        folded.add(row[2].casefold())
    del spelled

    raw = doc["settings"]
    if not isinstance(raw, dict):
        raise BackupError("settings must be an object.")
    settings = {}
    for key, value in raw.items():
        kind = type(DEFAULT_SETTINGS.get(key))
        if key in DEFAULT_SETTINGS and type(value) is not kind:
            raise BackupError(f"settings.{key}: expected {kind.__name__}.")
        try:
            settings[key] = rules.setting(key, value)
        except ValueError as exc:
            raise BackupError(f"settings.{key}: {exc}") from None
    return Backup(
        exported_at=exported_at,
        app_version=app_version,
        sources=sources,
        selections=selections,
        categories=categories,
        channel_edits=channel_edits,
        stream_prefs=stream_prefs,
        epg_mappings=epg_mappings,
        settings=settings,
    )


# ---------------------------------------------------------------- apply


def apply(engine: Engine, backup: Backup) -> int:
    """Replaces everything with the backup in one transaction and queues a refresh of every
    enabled source. Returns how many sources that is. Raises engine.Busy while the worker runs
    a job; any other failure leaves the data as it was."""
    settings = {**DEFAULT_SETTINGS, **backup.settings}

    def write(c) -> None:
        c.execute("DELETE FROM sources")  # cascades the cached playlists and guides
        for table in _USER_TABLES:
            c.execute(f"DELETE FROM {table}")
        c.execute(f"DELETE FROM kv WHERE key IN ({','.join('?' * len(_DERIVED_KV))})", _DERIVED_KV)
        for section, (table, columns, _) in _SECTIONS.items():
            rows = getattr(backup, section)
            c.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                rows,
            )
        c.execute(
            "INSERT INTO kv (key, value) VALUES ('settings', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(settings),),
        )
        if not settings["enrich_languages"]:
            c.execute("DELETE FROM meta_languages")

    enabled = [s[0] for s in backup.sources if s[4]]
    engine.replace_all(write, enabled)
    return len(enabled)
