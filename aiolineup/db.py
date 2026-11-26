"""SQLite persistence. One connection per thread, WAL mode, tiny schema."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 7

# Shared by SCHEMA and the v6 step, which rebuilds the table under a temporary name.
_SELECTIONS = """CREATE TABLE IF NOT EXISTS {table} (
    channel_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    added_at REAL NOT NULL,
    -- Sets the feed order and is sent as tvg-chno; unique, gaps allowed.
    number INTEGER NOT NULL UNIQUE CHECK (number >= 1)
);"""

# Shared by SCHEMA and the v7 step, like _SELECTIONS.
_CATEGORIES = """CREATE TABLE IF NOT EXISTS {table} (
    channel_key TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 1),  -- the admin's order, 1 first
    category TEXT NOT NULL,
    PRIMARY KEY (channel_key, position)
);"""

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('m3u', 'xmltv')),
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 100,
    created_at REAL NOT NULL,
    last_attempt REAL,
    last_success REAL,
    last_error TEXT,
    etag TEXT,
    last_modified TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    fragment_key TEXT
);

-- Last successfully parsed playlist entries per M3U source.
CREATE TABLE IF NOT EXISTS streams (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    pos INTEGER NOT NULL,
    channel_key TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    duration TEXT NOT NULL,
    attrs TEXT NOT NULL,
    directives TEXT NOT NULL,
    PRIMARY KEY (source_id, pos)
);
CREATE INDEX IF NOT EXISTS streams_channel ON streams(channel_key);

-- Whitelist. Survives refreshes; rows stay even while a channel is missing upstream.
"""
    + _SELECTIONS.format(table="selections")
    + """

-- The admin's stream order and on/off for a selected channel, forgotten on deselect.
-- signature = m3u.signature_key(url, directives). A stream without a row goes after the
-- ranked ones in source order, on.
CREATE TABLE IF NOT EXISTS stream_prefs (
    channel_key TEXT NOT NULL,
    signature TEXT NOT NULL,
    position INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    PRIMARY KEY (channel_key, signature)
);

-- The admin's own name and logo for a channel (NULL = the source value). Kept on deselect,
-- like category_overrides and epg_mappings.
CREATE TABLE IF NOT EXISTS channel_edits (
    channel_key TEXT PRIMARY KEY,
    name TEXT,
    logo TEXT
);

-- Manual channel -> XMLTV id overrides. Empty epg_id = "no guide for this channel".
-- source_id = the guide the id was picked from: tried first, other guides follow by
-- priority (NULL = priority order only, e.g. a typed id or a deleted source).
CREATE TABLE IF NOT EXISTS epg_mappings (
    channel_key TEXT PRIMARY KEY,
    epg_id TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL
);

-- The admin's own categories for a channel (1 to 8, in order); together they replace its
-- source categories in the app and are the group-title in the playlist feed. Keyed like
-- selections, so they survive refreshes. No rows = the source categories.
"""
    + _CATEGORIES.format(table="category_overrides")
    + """

-- Channel index of the last successfully parsed version of each XMLTV source.
CREATE TABLE IF NOT EXISTS epg_channels (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    epg_id TEXT NOT NULL,
    epg_id_lc TEXT NOT NULL,
    display_name TEXT NOT NULL,
    programmes INTEGER NOT NULL DEFAULT 0,
    first_start INTEGER,  -- programme time span of every id in the guide
    last_stop INTEGER,
    PRIMARY KEY (source_id, epg_id_lc)
);
CREATE INDEX IF NOT EXISTS epg_channels_lc ON epg_channels(epg_id_lc);

-- Programmes of every guide channel from scan time to 48 hours later, for "now playing".
CREATE TABLE IF NOT EXISTS epg_schedule (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    epg_id_lc TEXT NOT NULL,
    start INTEGER NOT NULL,
    stop INTEGER NOT NULL,
    title TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS epg_schedule_ch ON epg_schedule(source_id, epg_id_lc, start);

-- Languages from the iptv-org API, keyed by lowercase tvg-id (with and without @feed).
CREATE TABLE IF NOT EXISTS meta_languages (
    tvg_id_lc TEXT PRIMARY KEY,
    languages TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "epg_days": 7,  # future days of programmes to keep (0 = unlimited)
    "epg_past_hours": 6,  # keep programmes that ended at most this long ago
    "enrich_languages": True,  # fetch iptv-org feeds.json for language metadata
    "metadata_url": "https://iptv-org.github.io/api/feeds.json",
    "playlist_epg_header": False,  # add x-tvg-url pointing at our guide to the playlist
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        conn = self.conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(SCHEMA)  # idempotent; executescript manages its own transaction
        # Schema version this file was upgraded from in this run, else None; an upgrade
        # usually changes feed output, so the engine rebuilds the feeds at startup.
        self.upgraded_from: int | None = None
        if version == 0:  # fresh database: starts with no sources
            with conn as c:
                c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version < SCHEMA_VERSION:
            try:
                with conn as c:  # one transaction: a failed upgrade leaves the file as it was
                    self._migrate(c, version)
                    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except BaseException:
                self.close()
                raise
            self.upgraded_from = version

    @staticmethod
    def _migrate(c, version: int) -> None:
        if version < 2:
            cols = {r[1] for r in c.execute("PRAGMA table_info(epg_channels)")}
            for col in ("first_start", "last_stop"):
                if col not in cols:
                    c.execute(f"ALTER TABLE epg_channels ADD COLUMN {col} INTEGER")
        if version < 3:
            cols = {r[1] for r in c.execute("PRAGMA table_info(epg_mappings)")}
            if "source_id" not in cols:
                c.execute(
                    "ALTER TABLE epg_mappings ADD COLUMN source_id INTEGER"
                    " REFERENCES sources(id) ON DELETE SET NULL"
                )
        if version < 4:
            # epg_schedule is created by SCHEMA; clearing the filter key makes the worker
            # re-scan the cached guides once at startup, which fills it.
            c.execute("UPDATE sources SET fragment_key = NULL WHERE kind = 'xmltv'")
        if version < 5:
            # category_overrides is created by SCHEMA; no existing row is read or rewritten.
            pass
        if version < 6:
            # stream_prefs and channel_edits are created by SCHEMA; selections gains numbers.
            _number_selections(c)
        if version < 7:
            # category_overrides becomes an ordered list; each saved category is item 1.
            _list_categories(c)

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path, timeout=30, isolation_level=None, check_same_thread=True
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.create_function("fold", 1, _fold, deterministic=True)
            self._local.conn = _Conn(conn, self._write_lock)
            conn = self._local.conn
        return conn

    def close(self) -> None:
        """Close this thread's connection (others close when their thread ends)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------ helpers

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_kv(self, key: str, value: Any) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def settings(self) -> dict[str, Any]:
        stored = self.get_kv("settings", {}) or {}
        return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}


def _number_selections(c) -> None:
    """v6: rebuild selections with a number column. Existing rows get 1..N in the order the
    feed listed them up to v5: catalogue name folded, then channel key. The catalogue name is
    the one engine._build_catalog gives (first stream of an enabled playlist by priority); a
    channel missing upstream sorts by the name stored when it was selected."""
    from . import m3u

    names: dict[str, str] = {}
    for key, title, attrs in c.execute(
        """SELECT s.channel_key, s.title, s.attrs
           FROM streams s JOIN sources src ON src.id = s.source_id
           JOIN selections sel ON sel.channel_key = s.channel_key
           WHERE src.enabled = 1 AND src.kind = 'm3u'
           ORDER BY src.priority, src.id, s.pos"""
    ):
        if key not in names:
            entry = m3u.Entry(title=title, url="", attrs=json.loads(attrs))
            names[key] = m3u.channel_name(entry)
    rows = [tuple(r) for r in c.execute("SELECT channel_key, name, added_at FROM selections")]
    rows.sort(key=lambda r: (m3u.fold(names.get(r[0], r[1])), r[0]))
    c.execute(_SELECTIONS.format(table="selections_v6"))
    c.executemany(
        "INSERT INTO selections_v6 (channel_key, name, added_at, number) VALUES (?, ?, ?, ?)",
        [(*r, n) for n, r in enumerate(rows, 1)],
    )
    c.execute("DROP TABLE selections")
    c.execute("ALTER TABLE selections_v6 RENAME TO selections")


def _list_categories(c) -> None:
    """v7: rebuild category_overrides with a position column; every v5/v6 row is copied with
    the same key and text as position 1, so an edited channel's feed is unchanged. A v4 file
    had no such table: SCHEMA has just created the v7 one, and there is nothing to copy."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(category_overrides)")}
    if "position" in cols:
        return
    c.execute(_CATEGORIES.format(table="category_overrides_v7"))
    c.execute(
        "INSERT INTO category_overrides_v7 (channel_key, position, category)"
        " SELECT channel_key, 1, category FROM category_overrides"
    )
    c.execute("DROP TABLE category_overrides")
    c.execute("ALTER TABLE category_overrides_v7 RENAME TO category_overrides")


def _fold(value):
    """SQL fold(): accent- and case-insensitive text (SQLite's lower() is ASCII-only)."""
    from .m3u import fold

    return fold(value) if isinstance(value, str) else value


class _Conn:
    """sqlite3 connection wrapper: `with conn:` = serialized write transaction."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock
        self._depth = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._lock.acquire()
        try:
            if self._depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            # __exit__ never runs when __enter__ raises: release here or every
            # later write deadlocks.
            self._lock.release()
            raise
        self._depth += 1
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        self._depth -= 1
        try:
            if self._depth == 0:
                if exc_type:
                    self._conn.execute("ROLLBACK")
                else:
                    try:
                        self._conn.execute("COMMIT")
                    except BaseException:
                        if self._conn.in_transaction:
                            self._conn.execute("ROLLBACK")
                        raise
        finally:
            self._lock.release()
        return False
