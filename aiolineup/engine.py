"""Business logic: source refreshes, channel catalogue, feed generation, background worker.

All heavy work (downloads, parsing, building feeds) runs on one worker thread,
so refresh jobs can never overlap or pile up. HTTP handlers only enqueue work.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import fetch, m3u, xmltv
from .config import Config
from .db import Database

log = logging.getLogger("aiolineup")

EPG_DEBOUNCE_SECONDS = 15
# Re-scan an unchanged cached guide once its stored 48-hour schedule is this old.
SCHEDULE_STALE_SECONDS = 12 * 3600
MAX_JSON_OBJECT = 4 * 1024 * 1024  # one feeds.json object is a few hundred bytes
# AIOStreams' M3U addon limits (decimal; builtins/m3u at 89f03f6): playlist and decompressed
# guide bytes, programmes and channels it reads at most.
AIOSTREAMS_PLAYLIST_BYTES = 16_000_000
AIOSTREAMS_GUIDE_BYTES = 128_000_000
AIOSTREAMS_PROGRAMMES = 200_000
AIOSTREAMS_CHANNELS = 10_000
AIOSTREAMS_ID_UNITS = 256  # guide.ts drops ids longer than this (UTF-16 code units)
MAX_NUMBER = 9999  # highest channel number the admin can type
MAX_CATEGORIES = 8  # AIOStreams keeps the first 8 group-title parts of a channel
# Upstream attributes the playlist feed never passes on (any case): it writes its own.
_REPLACED_ATTRS = ("tvg-id", "tvg-name", "group-title", "tvg-chno", "ch-number")
_QUALITY_TAIL = re.compile(r"@(sd|hd|fhd|uhd|4k)$")


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _cut(text: str, units: int) -> str:
    return text.encode("utf-16-le", "surrogatepass")[: units * 2].decode("utf-16-le", "ignore")


def _clash_key(exported: str) -> tuple[str, str]:
    """(key, base) as AIOStreams compares ids: cut to 256 units, lowercase; base has the
    quality fallback (@sd|@hd|@fhd|@uhd|@4k at the end) removed."""
    key = _cut(exported, AIOSTREAMS_ID_UNITS).lower()
    return key, _QUALITY_TAIL.sub("", key)


def assign_export_ids(plain: list[tuple[str, str]]) -> dict[str, str]:
    """Maps (channel key, plain id) pairs, in selection order, to the ids the feeds export.

    The first channel keeps its plain id; a later one whose id AIOStreams would read as an
    earlier channel's (same lowercase id, or one equal to the other once its quality tail is
    removed) gets `~2`, `~3`, ... An id over 256 UTF-16 units becomes its first 240 units, `~`
    and 8 hex of the sha1 of the full id.
    """
    keys: set[str] = set()
    bases: set[str] = set()
    out: dict[str, str] = {}
    for channel_key, plain_id in plain:
        # As the playlist writes it, so ids that differ only in `"` vs `'` clash everywhere.
        plain_id = m3u.attr_value(plain_id)
        n = 1
        while True:
            candidate = plain_id if n == 1 else f"{plain_id}~{n}"
            if _units(candidate) > AIOSTREAMS_ID_UNITS:
                digest = hashlib.sha1(candidate.encode()).hexdigest()[:8]
                candidate = f"{_cut(candidate, 240)}~{digest}"
            key, base = _clash_key(candidate)
            if key not in keys and key not in bases and base not in keys:
                break
            n += 1
        keys.add(key)
        bases.add(base)
        out[channel_key] = candidate
    return out


def order_streams(rows, prefs: dict[str, tuple[int, bool]]) -> list[tuple[str, object, bool]]:
    """A channel's stream rows (source-priority order, with url and directives) as the feed
    lists them: [(signature, row, on)]. Duplicates (same signature) keep the first; streams
    with a saved choice come first by position, the rest follow in source order, on."""
    seen: set[str] = set()
    ranked, rest = [], []
    for row in rows:
        sig = m3u.signature_key(row["url"], json.loads(row["directives"]))
        if sig in seen:
            continue
        seen.add(sig)
        if sig in prefs:
            ranked.append((prefs[sig][0], sig, row, prefs[sig][1]))
        else:
            rest.append((sig, row, True))
    ranked.sort(key=lambda t: t[0])
    return [(sig, row, on) for _, sig, row, on in ranked] + rest


def feed_warnings(playlist: dict, epg: dict) -> list[str]:
    """Plain-language notes for each AIOStreams limit the built feeds pass."""
    out = []
    if (playlist.get("size_bytes") or 0) > AIOSTREAMS_PLAYLIST_BYTES:
        out.append(
            f"The playlist is over AIOStreams' default {AIOSTREAMS_PLAYLIST_BYTES // 1_000_000} MB"
            " limit. Select fewer channels."
        )
    if (playlist.get("streams") or 0) > AIOSTREAMS_CHANNELS:
        out.append(
            f"The playlist has more streams than AIOStreams' default {AIOSTREAMS_CHANNELS:,} entry"
            " limit, so it may leave some out. Select fewer channels."
        )
    if (epg.get("size_bytes") or 0) > AIOSTREAMS_GUIDE_BYTES:
        out.append(
            f"The guide is over AIOStreams' default {AIOSTREAMS_GUIDE_BYTES // 1_000_000} MB limit."
            " Reduce EPG days or selected channels."
        )
    if (epg.get("programmes") or 0) > AIOSTREAMS_PROGRAMMES:
        out.append(
            f"The guide has more programmes than AIOStreams' default {AIOSTREAMS_PROGRAMMES:,} limit."
            " Reduce EPG days or selected channels."
        )
    return out


@dataclass
class Channel:
    key: str
    name: str
    tvg_id: str
    out_id: str  # id used in the generated playlist and guide
    logo: str = ""
    categories: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    sources: list[int] = field(default_factory=list)
    streams: int = 0
    search: str = ""
    own_categories: list[str] = field(default_factory=list)  # the admin's list; replaces all
    # source categories when not empty
    own_name: str = ""  # the admin's name and logo (channel_edits); "" = the source value
    own_logo: str = ""

    @property
    def feed_name(self) -> str:
        """The title of every stream and the guide display-name. `name` stays the source name:
        exported ids and guide matching use it, so a rename never changes either."""
        return self.own_name or self.name

    @property
    def feed_logo(self) -> str:
        return self.own_logo or self.logo

    @property
    def shown_categories(self) -> list[str]:
        """What the app shows and filters by: the admin's list, else every source category."""
        return self.own_categories or self.categories

    @property
    def feed_categories(self) -> list[str]:
        """What group-title carries, `;`-joined: the shown list without case-insensitive
        repeats (first spelling kept), cut to the 8 AIOStreams reads."""
        out: list[str] = []
        seen: set[str] = set()
        for cat in self.shown_categories:
            if cat.casefold() not in seen:
                seen.add(cat.casefold())
                out.append(cat)
        return out[:MAX_CATEGORIES]

    def candidates(self) -> list[str]:
        """Automatic XMLTV ids to try, most specific first (xmltv.norm_id form)."""
        if not self.tvg_id:
            return [xmltv.norm_id(self.name)]  # not out_id: an exported suffix must not matter
        base, _ = m3u.split_tvg_id(self.tvg_id)
        out = [xmltv.norm_id(self.tvg_id)]
        if base and xmltv.norm_id(base) not in out:
            out.append(xmltv.norm_id(base))
        return out


class Busy(Exception):
    """The worker is running a job; try again when it finishes."""


class _EmptyPlaylist(Exception):
    """A downloaded playlist held no channels; rolls back the refresh transaction."""


class _SourceReplaced(Exception):
    """The source was deleted, or its id reused, while its guide was downloaded or scanned."""


class _ReaderAdapter(io.RawIOBase):
    """Lets TextIOWrapper decode a read()-only stream such as fetch.BoundedReader."""

    def __init__(self, reader):
        self._reader = reader

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        data = self._reader.read(len(buf))
        buf[: len(data)] = data
        return len(data)


def _split_list(value: str) -> list[str]:
    return [v.strip() for v in value.replace(";", ",").split(",") if v.strip()]


class Engine:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self._catalog: dict[str, Channel] | None = None
        self._catalog_lock = threading.Lock()
        # set_categories reads every spelling and then writes; two handler threads at once
        # could otherwise store "Kids" and "kids".
        self._category_lock = threading.Lock()
        self._cond = threading.Condition()
        self._pending_refresh: set[int] = set()
        self._pending_playlist = False
        self._pending_epg_at: float | None = None
        self._force_epg_scan = False
        self._refresh_metadata = False
        self.current_job: str | None = None
        # Held by the worker while it runs a job, and by replace_all while it swaps the data.
        self.job_lock = threading.Lock()
        self._generation = 0  # bumped by replace_all; a job taken before it is dropped
        self._stop = False
        self._thread: threading.Thread | None = None

    # ============================================================ paths

    @property
    def playlist_path(self) -> Path:
        return self.cfg.output_dir / "playlist.m3u"

    @property
    def epg_path(self) -> Path:
        return self.cfg.output_dir / "epg.xml"

    @property
    def epg_gz_path(self) -> Path:
        return self.cfg.output_dir / "epg.xml.gz"

    def raw_path(self, source_id: int) -> Path:
        return self.cfg.cache_dir / f"xmltv-{source_id}.raw"

    def fragment_path(self, source_id: int) -> Path:
        return self.cfg.cache_dir / f"xmltv-{source_id}.fragment.xml"

    def schedule_tmp_path(self, source_id: int) -> Path:
        return self.cfg.cache_dir / f"xmltv-{source_id}.schedule.tmp"

    # ============================================================ catalogue

    def invalidate_catalog(self) -> None:
        with self._catalog_lock:
            self._catalog = None

    def catalog(self) -> dict[str, Channel]:
        with self._catalog_lock:
            if self._catalog is None:
                self._catalog = self._build_catalog()
            return self._catalog

    def _build_catalog(self) -> dict[str, Channel]:
        languages = {
            r["tvg_id_lc"]: json.loads(r["languages"])
            for r in self.db.query("SELECT * FROM meta_languages")
        }
        rows = self.db.query(
            """SELECT s.source_id, s.channel_key, s.title, s.url, s.attrs, s.directives
               FROM streams s JOIN sources src ON src.id = s.source_id
               WHERE src.enabled = 1 AND src.kind = 'm3u'
               ORDER BY src.priority, src.id, s.pos"""
        )
        catalog: dict[str, Channel] = {}
        signatures: dict[str, set] = {}
        for row in rows:
            attrs = json.loads(row["attrs"])
            entry = m3u.Entry(
                title=row["title"],
                url=row["url"],
                attrs=attrs,
                directives=json.loads(row["directives"]),
            )
            key = row["channel_key"]
            ch = catalog.get(key)
            if ch is None:
                tvg_id = entry.tvg_id
                name = m3u.channel_name(entry)
                ch = catalog[key] = Channel(
                    key=key, name=name, tvg_id=tvg_id, out_id=tvg_id or name
                )
                signatures[key] = set()
            if not ch.logo:
                ch.logo = entry.attr("tvg-logo")
            for cat in m3u.categories(entry.group):
                if cat not in ch.categories:
                    ch.categories.append(cat)
            countries = _split_list(entry.attr("tvg-country").upper()) or (
                [m3u.country_from_id(entry.tvg_id)] if m3u.country_from_id(entry.tvg_id) else []
            )
            for c in countries:
                if c not in ch.countries:
                    ch.countries.append(c)
            langs = _split_list(entry.attr("tvg-language").lower())
            if not langs and entry.tvg_id:
                tid = entry.tvg_id.lower()
                langs = languages.get(tid) or languages.get(m3u.split_tvg_id(tid)[0]) or []
            for lang in langs:
                if lang not in ch.languages:
                    ch.languages.append(lang)
            if row["source_id"] not in ch.sources:
                ch.sources.append(row["source_id"])
            signatures[key].add(m3u.stream_signature(entry.url, entry.directives))
        own: dict[str, list[str]] = {}
        for r in self.db.query(
            "SELECT channel_key, category FROM category_overrides ORDER BY channel_key, position"
        ):
            own.setdefault(r["channel_key"], []).append(r["category"])
        edits = {r["channel_key"]: r for r in self.db.query("SELECT * FROM channel_edits")}
        for key, ch in catalog.items():
            ch.own_categories = own.get(key, [])
            ch.streams = len(signatures[key])
            edit = edits.get(key)
            if edit is not None:
                ch.own_name, ch.own_logo = edit["name"] or "", edit["logo"] or ""
            ch.search = m3u.fold(f"{ch.name} {ch.own_name} {ch.tvg_id}")
        return catalog

    def selected_keys(self) -> set[str]:
        return {r["channel_key"] for r in self.db.query("SELECT channel_key FROM selections")}

    def exported_ids(
        self, catalog: dict[str, Channel] | None = None, keys: list[str] | None = None
    ) -> dict[str, str]:
        """Channel key -> id exported in the playlist (tvg-id) and the guide (channel id), for
        every selected channel that is available. Computed at each call, never stored.
        `keys` is one read of `selection_order()`, so a caller derives its rows and ids from
        the same snapshot."""
        catalog = self.catalog() if catalog is None else catalog
        if keys is None:
            keys = self.selection_order()
        return assign_export_ids([(k, catalog[k].out_id) for k in keys if k in catalog])

    def selection_order(self) -> list[str]:
        """Selected keys in the order exported ids are assigned (added_at, never the number)."""
        return list(self.selection_numbers())

    def selection_numbers(self) -> dict[str, int]:
        """Selected key -> channel number, in selection_order. One read, so a caller can take
        its rows, numbers and exported ids from the same snapshot."""
        rows = self.db.query(
            "SELECT channel_key, number FROM selections ORDER BY added_at, channel_key"
        )
        return {r["channel_key"]: r["number"] for r in rows}

    @staticmethod
    def _next_number(c) -> int:
        """Highest number + 1; past MAX_NUMBER the lowest free one (still past it when every
        number is taken, so selecting never fails)."""
        top = c.execute("SELECT coalesce(max(number), 0) FROM selections").fetchone()[0]
        if top < MAX_NUMBER:
            return top + 1
        free = c.execute(
            "SELECT min(n) FROM (SELECT 1 AS n UNION ALL SELECT number + 1 FROM selections)"
            " WHERE n <= ? AND n NOT IN (SELECT number FROM selections)",
            (MAX_NUMBER,),
        ).fetchone()[0]
        return free or top + 1

    def _shown_name(self, key: str, stored: str, catalog: dict[str, Channel] | None) -> str:
        catalog = self.catalog() if catalog is None else catalog
        if key in catalog:
            return catalog[key].feed_name
        edit = self.db.one("SELECT name FROM channel_edits WHERE channel_key = ?", (key,))
        return (edit["name"] if edit else "") or stored

    def set_selection(self, select: list[str], deselect: list[str]) -> int:
        """Selects in the given order: each new channel gets the next number. Deselecting
        forgets the number and stream choices; name, logo, category and guide mapping stay."""
        catalog = self.catalog()
        now = time.time()
        changed = 0
        ids_before = self.exported_ids(catalog)
        with self.db.conn() as c:
            for key in select:
                ch = catalog.get(key)
                if ch is None:
                    continue  # only channels that exist can be newly selected
                if c.execute("SELECT 1 FROM selections WHERE channel_key = ?", (key,)).fetchone():
                    continue
                c.execute(
                    "INSERT INTO selections (channel_key, name, added_at, number) VALUES (?, ?, ?, ?)",
                    (key, ch.name, now, self._next_number(c)),
                )
                changed += 1
            for key in deselect:
                cur = c.execute("DELETE FROM selections WHERE channel_key = ?", (key,))
                if cur.rowcount:
                    c.execute("DELETE FROM stream_prefs WHERE channel_key = ?", (key,))
                    changed += 1
        if changed:
            self.request_playlist()
            # A kept channel whose exported id moved (a clashing one was deselected) must not
            # borrow the old holder's schedule while the debounce runs: rebuild the guide now.
            ids_after = self.exported_ids(catalog)
            moved = any(ids_after[k] != v for k, v in ids_before.items() if k in ids_after)
            self.request_epg(delay=0 if moved else EPG_DEBOUNCE_SECONDS)
        return changed

    def set_categories(self, key: str, categories: list[str] | None) -> list[str] | None:
        """Replaces (or with None resets to the source ones) a channel's own category list,
        1 to MAX_CATEGORIES items in the given order. Returns the stored list.

        An item equal to another channel's category ignoring case takes that category's
        spelling; the edited channel's own categories are skipped so it can be re-cased.
        Two items equal ignoring case are refused (ValueError).
        """
        with self._category_lock:
            stored = self._normalise_categories(key, categories)
            with self.db.conn() as c:
                self._write_categories(c, key, stored)
            self.invalidate_catalog()
        if key in self.selected_keys():  # only selected channels are in the feed
            self.request_playlist()
        return stored

    def _normalise_categories(self, key: str, categories: list[str] | None) -> list[str] | None:
        catalog = self.catalog()
        if key not in catalog:
            raise KeyError(key)
        if categories is None:
            return None
        if not 1 <= len(categories) <= MAX_CATEGORIES:
            raise ValueError(f"A channel takes 1 to {MAX_CATEGORIES} categories.")
        spelling: dict[str, str] = {}
        for other_key, ch in catalog.items():
            if other_key != key:
                for cat in ch.shown_categories:
                    spelling.setdefault(cat.casefold(), cat)
        if len({c.casefold() for c in categories}) < len(categories):
            raise ValueError("This channel already has that category.")
        return [spelling.get(c.casefold(), c) for c in categories]

    @staticmethod
    def _write_categories(c, key: str, categories: list[str] | None) -> None:
        c.execute("DELETE FROM category_overrides WHERE channel_key = ?", (key,))
        c.executemany(
            "INSERT INTO category_overrides (channel_key, position, category) VALUES (?, ?, ?)",
            [(key, i, cat) for i, cat in enumerate(categories or [], 1)],
        )

    def set_number(self, key: str, number: int) -> None:
        """Gives a selected channel another number. A number held by another channel is
        refused (ValueError naming it); nothing else is renumbered."""
        catalog = self.catalog()
        with self.db.conn() as c:
            row = c.execute(
                "SELECT number FROM selections WHERE channel_key = ?", (key,)
            ).fetchone()
            if row is None:
                raise KeyError(key)
            if row["number"] == number:
                return
            holder = c.execute(
                "SELECT channel_key, name FROM selections WHERE number = ?", (number,)
            ).fetchone()
            if holder is not None:
                name = self._shown_name(holder["channel_key"], holder["name"], catalog)
                raise ValueError(f"Number {number} is used by {name}.")
            c.execute("UPDATE selections SET number = ? WHERE channel_key = ?", (number, key))
        self.request_playlist()

    def stream_prefs(self, keys: list[str]) -> dict[str, dict[str, tuple[int, bool]]]:
        """Channel key -> {signature: (position, enabled)} for channels with saved choices."""
        out: dict[str, dict[str, tuple[int, bool]]] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            for r in self.db.query(
                "SELECT channel_key, signature, position, enabled FROM stream_prefs"
                f" WHERE channel_key IN ({','.join('?' * len(chunk))})",
                chunk,
            ):
                out.setdefault(r["channel_key"], {})[r["signature"]] = (
                    r["position"],
                    bool(r["enabled"]),
                )
        return out

    def channel_streams(self, key: str, prefs: dict[str, tuple[int, bool]] | None = None):
        """A channel's distinct streams from enabled playlists in feed order, switched-off ones
        included: [(signature, row, on)]. `prefs` replaces the saved choices."""
        rows = self.db.query(
            """SELECT s.title, s.url, s.duration, s.attrs, s.directives, src.name AS source
               FROM streams s JOIN sources src ON src.id = s.source_id
               WHERE src.enabled = 1 AND src.kind = 'm3u' AND s.channel_key = ?
               ORDER BY src.priority, src.id, s.pos""",
            (key,),
        )
        if prefs is None:
            prefs = self.stream_prefs([key]).get(key, {})
        return order_streams(rows, prefs)

    def set_streams(self, key: str, choices: list[tuple[str, bool]] | None) -> None:
        """Saves a selected channel's stream order and on/off ([(signature, on)], first =
        first in the feed), or with None goes back to source order. Streams not listed go
        after the listed ones in source order, on. A saved choice for a stream the editor
        cannot see (its playlist is disabled, or it is gone upstream) is kept after the listed
        ones, so it holds when the stream returns; None forgets those too. Refused (ValueError)
        when it leaves no present stream on."""
        with self.db.conn() as c:
            if not c.execute("SELECT 1 FROM selections WHERE channel_key = ?", (key,)).fetchone():
                raise KeyError(key)
            if choices is not None:
                known = {
                    m3u.signature_key(r["url"], json.loads(r["directives"]))
                    for r in c.execute(
                        "SELECT url, directives FROM streams WHERE channel_key = ?", (key,)
                    )
                }
                sigs = [sig for sig, _ in choices]
                if len(set(sigs)) != len(sigs):
                    raise ValueError("A stream is listed twice.")
                if not set(sigs) <= known:
                    raise ValueError("Unknown stream for this channel.")
                prefs = {sig: (i, on) for i, (sig, on) in enumerate(choices)}
                present = self.channel_streams(key, prefs)
                if present and not any(on for _, _, on in present):
                    raise ValueError("At least one stream must stay on.")
                shown = {sig for sig, _, _ in present} | set(sigs)
                choices = choices + [
                    (r["signature"], bool(r["enabled"]))
                    for r in c.execute(
                        "SELECT signature, enabled FROM stream_prefs WHERE channel_key = ?"
                        " ORDER BY position",
                        (key,),
                    )
                    if r["signature"] not in shown
                ]
            c.execute("DELETE FROM stream_prefs WHERE channel_key = ?", (key,))
            if choices is not None:
                c.executemany(
                    "INSERT INTO stream_prefs (channel_key, signature, position, enabled)"
                    " VALUES (?, ?, ?, ?)",
                    [(key, sig, i, int(on)) for i, (sig, on) in enumerate(choices)],
                )
        self.request_playlist()

    def set_edit(self, key: str, **fields: str | list[str] | None) -> dict:
        """Sets the admin's own `name`, `logo` and/or `categories` for a channel in one
        transaction; None or "" goes back to the source value (None for categories). A field
        not passed is kept. Returns the stored values. Categories need the channel in the
        catalog and are refused (ValueError) as in `set_categories`; nothing is written then."""
        if key not in self.catalog() and key not in self.selected_keys():
            raise KeyError(key)
        with self._category_lock:
            has_cats = "categories" in fields
            cats = self._normalise_categories(key, fields["categories"]) if has_cats else None
            with self.db.conn() as c:
                row = c.execute(
                    "SELECT name, logo FROM channel_edits WHERE channel_key = ?", (key,)
                ).fetchone()
                stored = (
                    {"name": row["name"], "logo": row["logo"]}
                    if row
                    else {"name": None, "logo": None}
                )
                stored.update({k: v or None for k, v in fields.items() if k in stored})
                if stored["name"] is None and stored["logo"] is None:
                    c.execute("DELETE FROM channel_edits WHERE channel_key = ?", (key,))
                else:
                    c.execute(
                        "INSERT INTO channel_edits (channel_key, name, logo) VALUES (?, ?, ?)"
                        " ON CONFLICT(channel_key) DO UPDATE SET name = excluded.name,"
                        " logo = excluded.logo",
                        (key, stored["name"], stored["logo"]),
                    )
                if has_cats:
                    self._write_categories(c, key, cats)
            self.invalidate_catalog()
        if has_cats:
            stored["categories"] = cats
        if key in self.selected_keys():  # only selected channels are in the feeds
            self.request_playlist()
            self.request_epg(delay=EPG_DEBOUNCE_SECONDS)
        return stored

    # ============================================================ refresh: M3U

    def _mark(self, source_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db.conn() as c:
            c.execute(f"UPDATE sources SET {cols} WHERE id = ?", (*fields.values(), source_id))

    def m3u_download_path(self, source_id: int) -> Path:
        return self.cfg.cache_dir / f"m3u-{source_id}.download"

    def refresh_m3u(self, src) -> bool:
        """Returns True if the stored channel list changed.

        The playlist is downloaded to a temp file and parsed line by line straight into the
        database, so memory stays flat however long the list is. Old rows are replaced in the
        same transaction: a refresh that fails or finds nothing keeps them.
        """
        now = time.time()
        sid = src["id"]
        tmp = self.m3u_download_path(sid)
        stats = m3u.ParseStats()
        count = 0

        def rows(lines):
            nonlocal count
            for e in m3u.parse(lines, stats):
                yield (
                    sid,
                    count,
                    m3u.channel_key(e),
                    e.title,
                    e.url,
                    e.duration,
                    json.dumps(e.attrs, ensure_ascii=False),
                    json.dumps(e.directives, ensure_ascii=False),
                )
                count += 1

        try:
            result = fetch.download_to_file(
                src["url"],
                tmp,
                timeout=self.cfg.request_timeout,
                max_bytes=self.cfg.m3u_max_bytes,
                allow_private=self.cfg.allow_private_sources,
                etag=src["etag"],
                last_modified=src["last_modified"],
            )
            fh, reader = fetch.open_maybe_gzip(tmp, self.cfg.m3u_max_bytes)
            try:
                text = io.TextIOWrapper(
                    io.BufferedReader(_ReaderAdapter(reader)), encoding="utf-8", errors="replace"
                )
                with self.db.conn() as c:
                    # The id may have been reused by a new source while we downloaded.
                    if not c.execute(
                        "SELECT 1 FROM sources WHERE id = ? AND created_at = ?",
                        (sid, src["created_at"]),
                    ).fetchone():
                        raise sqlite3.IntegrityError("source was replaced")
                    c.execute("DELETE FROM streams WHERE source_id = ?", (sid,))
                    c.executemany(
                        "INSERT INTO streams (source_id, pos, channel_key, title, url, duration, attrs, directives)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        rows(text),
                    )
                    if not count:
                        raise _EmptyPlaylist
                    c.execute(
                        "UPDATE sources SET last_attempt = ?, last_success = ?, last_error = ?, etag = ?,"
                        " last_modified = ?, item_count = ?, size_bytes = ? WHERE id = ?",
                        (
                            now,
                            now,
                            f"Skipped {stats.skipped} malformed entries."
                            if stats.skipped
                            else None,
                            result.etag,
                            result.last_modified,
                            count,
                            result.size,
                            sid,
                        ),
                    )
            finally:
                fh.close()
        except fetch.NotModified:
            self._mark(sid, last_attempt=now, last_success=now, last_error=None)
            return False
        except _EmptyPlaylist:
            self._mark(
                sid,
                last_attempt=now,
                last_error=f"No channels found in playlist{self._html_hint(tmp)}.",
            )
            return False
        except sqlite3.IntegrityError:
            if (
                self.db.one(
                    "SELECT 1 FROM sources WHERE id = ? AND created_at = ?",
                    (sid, src["created_at"]),
                )
                is None
            ):
                log.info("source %s was deleted or replaced during its refresh", sid)
                return False
            raise
        except (fetch.FetchError, ValueError, OSError, EOFError, zlib.error) as exc:
            self._mark(sid, last_attempt=now, last_error=str(exc))
            log.warning("source %s refresh failed: %s", sid, exc)
            return False
        finally:
            tmp.unlink(missing_ok=True)
        self.invalidate_catalog()
        log.info("source %s: %d entries (%d skipped)", sid, count, stats.skipped)
        return True

    @staticmethod
    def _html_hint(path: Path) -> str:
        try:
            fh, reader = fetch.open_maybe_gzip(path, 1024 * 1024)
            with fh:
                head = reader.read(2048)
        except (OSError, EOFError, zlib.error, fetch.FetchError):
            return ""
        return " (looks like HTML, not a playlist)" if b"<html" in head.lower() else ""

    # ============================================================ refresh: metadata

    def refresh_languages(self) -> None:
        settings = self.db.settings()
        if not settings["enrich_languages"]:
            self.clear_metadata_error()
            return
        wanted = set()
        for row in self.db.query(
            "SELECT DISTINCT channel_key FROM streams WHERE channel_key LIKE 'id:%'"
        ):
            tid = row["channel_key"][3:]
            wanted.add(tid)
            wanted.add(m3u.split_tvg_id(tid)[0])
        wanted_key = hashlib.sha1("\n".join(sorted(wanted)).encode()).hexdigest()
        state = self.db.get_kv("metadata_state", {}) or {}
        if state.get("url") != settings["metadata_url"] or state.get("wanted_key") != wanted_key:
            state = {}  # channel set changed: the cached result is incomplete, fetch again
        tmp = self.cfg.cache_dir / "metadata.download"
        try:
            result = fetch.download_to_file(
                settings["metadata_url"],
                tmp,
                timeout=self.cfg.request_timeout,
                max_bytes=64 * 1024 * 1024,
                allow_private=self.cfg.allow_private_sources,
                etag=state.get("etag"),
            )
            fh, reader = fetch.open_maybe_gzip(tmp, 256 * 1024 * 1024)
            try:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                langs = parse_feed_languages(
                    lambda n: decoder.decode(reader.read(n), final=False), wanted
                )
            finally:
                fh.close()
        except fetch.NotModified:
            return
        except (fetch.FetchError, ValueError, OSError, EOFError, zlib.error) as exc:
            with self.db.conn():
                if self._lookup_changed(settings):
                    return
                self.db.set_kv(
                    "metadata_state",
                    {**state, "error": str(exc), "attempt": time.time()},
                )
            return
        finally:
            tmp.unlink(missing_ok=True)
        with self.db.conn() as c:
            # The lookup may have been disabled, or pointed elsewhere, during the download.
            if self._lookup_changed(settings):
                return
            c.execute("DELETE FROM meta_languages")
            c.executemany(
                "INSERT INTO meta_languages (tvg_id_lc, languages) VALUES (?, ?)",
                [(k, json.dumps(v)) for k, v in langs.items()],
            )
            self.db.set_kv(
                "metadata_state",
                {
                    "url": settings["metadata_url"],
                    "etag": result.etag,
                    "wanted_key": wanted_key,
                    "error": None,
                    "success": time.time(),
                    "entries": len(langs),
                },
            )
        self.invalidate_catalog()

    def _lookup_changed(self, started_with: dict) -> bool:
        """Was the language lookup disabled or re-pointed since `started_with` was read?"""
        now = self.db.settings()
        return not now["enrich_languages"] or now["metadata_url"] != started_with["metadata_url"]

    def clear_metadata_error(self) -> None:
        """Forget the lookup state (error, etag): after a disable the cached rows are gone,
        so a later re-enable must fetch again instead of trusting a 304."""
        if self.db.get_kv("metadata_state"):
            self.db.set_kv("metadata_state", {})

    # ============================================================ refresh: XMLTV

    def _epg_plan(self):
        """Selected, currently available channels with their XMLTV id candidates and
        the guide source a manual mapping was picked from (None = priority order)."""
        catalog = self.catalog()
        export_ids = self.exported_ids(catalog)  # one selection read for plan and ids
        mappings = {r["channel_key"]: r for r in self.db.query("SELECT * FROM epg_mappings")}
        plan: list[tuple[Channel, list[str], int | None]] = []
        for key in sorted(export_ids):
            ch = catalog[key]
            m = mappings.get(key)
            if m is not None:
                cands = [xmltv.norm_id(m["epg_id"])] if m["epg_id"].strip() else []
                plan.append((ch, cands, m["source_id"]))
            else:
                plan.append((ch, ch.candidates(), None))
        wanted = {c for _, cands, _ in plan for c in cands}
        # The key also covers the id normalisation and the scan's index (v2: a time span for
        # every id), so an upgrade re-filters cached guides.
        wanted_key = hashlib.sha1(
            "\n".join([f"norm:{xmltv.NORM_VERSION}", "scan:2", *sorted(wanted)]).encode()
        ).hexdigest()
        return plan, wanted, wanted_key, export_ids

    def _store_scan(
        self,
        source_id: int,
        created_at: float,
        scan: xmltv.ScanResult,
        wanted_key: str,
        **fields,
    ) -> None:
        sched = self.schedule_tmp_path(source_id)
        try:
            self._store_scan_rows(source_id, created_at, scan, wanted_key, sched, fields)
        finally:
            sched.unlink(missing_ok=True)

    def _store_scan_rows(
        self,
        source_id: int,
        created_at: float,
        scan: xmltv.ScanResult,
        wanted_key: str,
        sched: Path,
        fields: dict,
    ) -> None:
        def schedule_rows(fh):
            for line in fh:
                lc, start, stop, title = json.loads(line)
                yield source_id, lc, start, stop, title

        with self.db.conn() as c:
            # The id may have been reused by a new source while we downloaded or scanned.
            if not c.execute(
                "SELECT 1 FROM sources WHERE id = ? AND created_at = ?", (source_id, created_at)
            ).fetchone():
                raise _SourceReplaced
            c.execute("DELETE FROM epg_schedule WHERE source_id = ?", (source_id,))
            if sched.exists():
                with open(sched, encoding="utf-8", newline="\n") as fh:
                    c.executemany(
                        "INSERT INTO epg_schedule (source_id, epg_id_lc, start, stop, title)"
                        " VALUES (?, ?, ?, ?, ?)",
                        schedule_rows(fh),
                    )
            c.execute("DELETE FROM epg_channels WHERE source_id = ?", (source_id,))
            c.executemany(
                "INSERT INTO epg_channels (source_id, epg_id, epg_id_lc, display_name, programmes, first_start, last_stop)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        source_id,
                        e.epg_id,
                        lc,
                        e.display_name,
                        e.programmes,
                        e.first_start,
                        e.last_stop,
                    )
                    for lc, e in scan.index.items()
                ],
            )
            fields = {"fragment_key": wanted_key, "item_count": len(scan.index), **fields}
            cols = ", ".join(f"{k} = ?" for k in fields)
            c.execute(f"UPDATE sources SET {cols} WHERE id = ?", (*fields.values(), source_id))
            scanned = self.db.get_kv("schedule_scanned", {}) or {}
            scanned[str(source_id)] = time.time()
            self.db.set_kv("schedule_scanned", scanned)

    def _schedule_stale(self, source_id: int) -> bool:
        """Has the stored 48-hour schedule of this guide aged past SCHEDULE_STALE_SECONDS?
        An unchanged guide (HTTP 304) is then re-scanned from the cached copy."""
        scanned = (self.db.get_kv("schedule_scanned", {}) or {}).get(str(source_id))
        return scanned is None or time.time() - scanned > SCHEDULE_STALE_SECONDS

    def refresh_xmltv(self, src) -> bool:
        now = time.time()
        sid = src["id"]
        raw = self.raw_path(sid)
        tmp_raw = raw.with_suffix(".download")
        tmp_frag = self.fragment_path(sid).with_suffix(".tmp")
        try:
            result = fetch.download_to_file(
                src["url"],
                tmp_raw,
                timeout=self.cfg.request_timeout,
                max_bytes=self.cfg.epg_max_download_bytes,
                allow_private=self.cfg.allow_private_sources,
                etag=src["etag"] if raw.exists() else None,
                last_modified=src["last_modified"] if raw.exists() else None,
            )
            _, wanted, wanted_key, _ = self._epg_plan()
            scan = xmltv.scan(
                tmp_raw,
                wanted,
                tmp_frag,
                self.cfg.epg_max_decompressed_bytes,
                schedule_path=self.schedule_tmp_path(sid),
            )
        except fetch.NotModified:
            self._mark(sid, last_attempt=now, last_success=now, last_error=None)
            return self._schedule_stale(sid)  # True makes build_epg re-scan the cached copy
        except (fetch.FetchError, xmltv.GuideError, ValueError) as exc:
            self.schedule_tmp_path(sid).unlink(missing_ok=True)
            self._mark(sid, last_attempt=now, last_error=str(exc))
            log.warning("guide source %s refresh failed: %s", sid, exc)
            return False
        # Store before swapping the files: if storing fails, the transaction rolls back and
        # the previous raw guide and fragment stay in place (the caller removes the temp files).
        try:
            self._store_scan(
                sid,
                src["created_at"],
                scan,
                wanted_key,
                last_attempt=now,
                last_success=now,
                last_error=None,
                etag=result.etag,
                last_modified=result.last_modified,
                size_bytes=result.size,
            )
        except _SourceReplaced:
            log.info("guide source %s was deleted or replaced during its refresh", sid)
            return False
        except (sqlite3.Error, ValueError, OSError) as exc:
            self._mark(sid, last_attempt=now, last_error=f"Could not store guide: {exc}")
            log.warning("guide source %s store failed: %s", sid, exc)
            return False
        try:
            os.replace(tmp_raw, raw)
            os.replace(tmp_frag, self.fragment_path(sid))
        except OSError as exc:
            # The scan is committed but the files are not in step: clear the keys so the next
            # build re-scans whatever raw file is in place and no 304 trusts an old guide.
            self._mark(
                sid,
                fragment_key=None,
                etag=None,
                last_modified=None,
                last_error=f"Could not save guide: {exc}",
            )
            log.warning("guide source %s file swap failed: %s", sid, exc)
            return True
        log.info(
            "guide source %s: %d channels, %d programmes kept, %.0f MiB decompressed",
            sid,
            len(scan.index),
            scan.kept,
            scan.decompressed_bytes / 2**20,
        )
        return True

    def _cleanup_tmp(self, sid: int) -> None:
        for p in (
            self.raw_path(sid).with_suffix(".download"),
            self.fragment_path(sid).with_suffix(".tmp"),
            self.schedule_tmp_path(sid),
        ):
            p.unlink(missing_ok=True)

    # ============================================================ builds

    def build_playlist(self) -> dict:
        catalog = self.catalog()
        numbers = self.selection_numbers()  # one read: rows, numbers and ids agree
        export_ids = self.exported_ids(catalog, list(numbers))
        selected = [catalog[k] for k in export_ids]
        selected.sort(key=lambda ch: numbers[ch.key])
        prefs = self.stream_prefs([ch.key for ch in selected])
        settings = self.db.settings()
        header = "#EXTM3U"
        if settings["playlist_epg_header"] and self.cfg.public_url:
            header += f' x-tvg-url="{self.cfg.public_url}/feed/{self.cfg.feed_token}/epg.xml"'
        streams = 0
        written: set[str] = set()
        tmp = self.playlist_path.with_name("playlist.m3u.tmp")
        by_key: dict[str, list] = {}
        if selected:
            keys = [ch.key for ch in selected]
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                rows = self.db.query(
                    f"""SELECT s.channel_key, s.title, s.url, s.duration, s.attrs, s.directives
                        FROM streams s JOIN sources src ON src.id = s.source_id
                        WHERE src.enabled = 1 AND s.channel_key IN ({",".join("?" * len(chunk))})
                        ORDER BY src.priority, src.id, s.pos""",
                    chunk,
                )
                for row in rows:
                    by_key.setdefault(row["channel_key"], []).append(row)
        with open(tmp, "w", encoding="utf-8", newline="\n") as out:
            out.write(header + "\n")
            for ch in selected:
                for _, row, on in order_streams(by_key.get(ch.key, []), prefs.get(ch.key, {})):
                    if not on:
                        continue
                    directives = json.loads(row["directives"])
                    attrs = json.loads(row["attrs"])
                    lower = {k.lower() for k, v in attrs.items() if v.strip()}
                    # One id and one title per channel, the same in the guide: AIOStreams merges
                    # streams only when tvg-id and title both match, and falls back to
                    # tvg-name for guide matching, so upstream ids, titles and tvg-name never
                    # pass through. The channel number is ours alone: upstream tvg-chno and
                    # ch-number (AIOStreams' fallback) are dropped.
                    attrs = {
                        "tvg-id": export_ids[ch.key],
                        "tvg-chno": str(numbers[ch.key]),
                        **{
                            k: v
                            for k, v in attrs.items()
                            if k.lower() not in _REPLACED_ATTRS
                            and not (ch.own_logo and k.lower() == "tvg-logo")
                        },
                    }
                    # The channel's categories, the same on every stream; AIOStreams splits on
                    # `;`. Upstream group-title and #EXTGRP never pass through.
                    if ch.feed_categories:
                        attrs["group-title"] = ";".join(ch.feed_categories)
                    # Any spelling m3u.py keeps as a directive (#EXTGRP:, #EXTGRP :, bare).
                    directives = [d for d in directives if not d.upper().startswith("#EXTGRP")]
                    if "tvg-country" not in lower and ch.countries:
                        attrs["tvg-country"] = ";".join(ch.countries)
                    if "tvg-language" not in lower and ch.languages:
                        attrs["tvg-language"] = ";".join(ch.languages)
                    if ch.own_logo:  # the admin's logo replaces every stream's
                        attrs["tvg-logo"] = ch.own_logo
                    elif "tvg-logo" not in lower and ch.logo:
                        attrs["tvg-logo"] = ch.logo
                    out.write(
                        m3u.render_entry(
                            ch.feed_name, row["url"], attrs, directives, row["duration"]
                        )
                    )
                    streams += 1
                    written.add(ch.key)
        os.replace(tmp, self.playlist_path)
        stats = {
            "built_at": time.time(),
            # Channels the file lists: one whose on streams all vanished upstream is not in it.
            "channels": len(written),
            "streams": streams,
            "size_bytes": self.playlist_path.stat().st_size,
        }
        self.db.set_kv("playlist_stats", stats)
        return stats

    def build_epg(self, force_scan: bool = False) -> dict:
        plan, wanted, wanted_key, export_ids = self._epg_plan()
        sources = self.db.query(
            "SELECT * FROM sources WHERE kind = 'xmltv' AND enabled = 1 ORDER BY priority, id"
        )
        errors = {}
        # Re-filter cached upstream guides whose candidate set is out of date.
        for src in sources:
            sid = src["id"]
            raw = self.raw_path(sid)
            if not raw.exists():
                continue
            if (
                not force_scan
                and src["fragment_key"] == wanted_key
                and self.fragment_path(sid).exists()
                and not self._schedule_stale(sid)
            ):
                continue
            self.current_job = f"Filtering guide: {src['name']}"
            tmp = self.fragment_path(sid).with_suffix(".tmp")
            try:
                scan = xmltv.scan(
                    raw,
                    wanted,
                    tmp,
                    self.cfg.epg_max_decompressed_bytes,
                    schedule_path=self.schedule_tmp_path(sid),
                )
                self._store_scan(sid, src["created_at"], scan, wanted_key)
                try:
                    os.replace(tmp, self.fragment_path(sid))
                except OSError:
                    self._mark(sid, fragment_key=None)  # the next build re-scans
                    raise
            except _SourceReplaced:
                log.info("guide source %s was deleted or replaced during the build", sid)
                continue
            except (xmltv.GuideError, fetch.FetchError, sqlite3.Error, OSError) as exc:
                if isinstance(exc, sqlite3.IntegrityError) and not self.db.one(
                    "SELECT 1 FROM sources WHERE id = ?", (sid,)
                ):
                    log.info("guide source %s was deleted during the build", sid)
                else:
                    errors[sid] = str(exc)
                continue
            finally:
                tmp.unlink(missing_ok=True)
                self.schedule_tmp_path(sid).unlink(missing_ok=True)

        not_before, not_after = self._epg_window()
        # One guide source per channel: the first source (priority order) that has
        # programmes inside the output window. A source that only lists the channel,
        # or only has stale / far-future programmes, falls through to the next one.
        usable: dict[tuple[int, str], dict] = {}
        if wanted and sources:
            ids = [s["id"] for s in sources]
            for row in self.db.query(
                "SELECT source_id, epg_id, epg_id_lc, programmes, first_start, last_stop FROM epg_channels"
                f" WHERE source_id IN ({','.join('?' * len(ids))})",
                ids,
            ):
                if row["epg_id_lc"] in wanted and _in_window(row, not_before, not_after):
                    usable[(row["source_id"], row["epg_id_lc"])] = dict(row)
        routes: dict[tuple[int, str], list[str]] = {}
        coverage: dict[str, dict] = {}
        for ch, cands, pinned in plan:
            # A mapping picked from a specific guide tries that guide first.
            order = sorted(sources, key=lambda s: s["id"] != pinned) if pinned else sources
            match = next(
                ((s["id"], c) for s in order for c in cands if (s["id"], c) in usable), None
            )
            if match:
                routes.setdefault(match, []).append(export_ids[ch.key])
                coverage[ch.key] = {"source_id": match[0], "epg_id": usable[match]["epg_id"]}

        self.current_job = "Writing guide"
        stats = xmltv.assemble(
            [(s["id"], self.fragment_path(s["id"])) for s in sources],
            routes,
            [
                xmltv.OutputChannel(export_ids[ch.key], ch.feed_name, ch.feed_logo)
                for ch, _, _ in plan
            ],
            self.epg_path,
            self.epg_gz_path,
            not_before=not_before,
            not_after=not_after,
        )
        for ch, _, _ in plan:
            if ch.key in coverage:
                coverage[ch.key]["programmes"] = stats.per_channel.get(export_ids[ch.key], 0)
        result = {
            "built_at": time.time(),
            "selected": len(plan),
            "covered": len(coverage),
            "channels": stats.channels,
            "programmes": stats.programmes,
            "duplicates": stats.duplicates,
            "outside_window": stats.outside_window,
            "size_bytes": stats.size_bytes,
            "gz_size_bytes": stats.gz_size_bytes,
            "errors": errors,
        }
        self.db.set_kv("epg_stats", result)
        self.db.set_kv("epg_coverage", coverage)
        return result

    def _epg_window(self) -> tuple[int, int | None]:
        settings = self.db.settings()
        now = int(time.time())
        days = int(settings["epg_days"])
        return now - int(settings["epg_past_hours"]) * 3600, (
            now + days * 86400 if days > 0 else None
        )

    # ============================================================ diagnostics

    def guide_source_status(self) -> list[dict]:
        """Per XMLTV source: did it download and parse, and how much does it hold."""
        counts = {
            r["source_id"]: r
            for r in self.db.query(
                "SELECT source_id, count(*) AS channels, sum(programmes > 0) AS with_programmes"
                " FROM epg_channels GROUP BY source_id"
            )
        }
        out = []
        for src in self.db.query(
            "SELECT * FROM sources WHERE kind = 'xmltv' ORDER BY priority, id"
        ):
            c = counts.get(src["id"])
            out.append(
                {
                    "id": src["id"],
                    "name": src["name"],
                    "enabled": bool(src["enabled"]),
                    "cached": self.raw_path(src["id"]).exists(),
                    "last_success": src["last_success"],
                    "error": src["last_error"],
                    "channels": c["channels"] if c else 0,
                    "with_programmes": (c["with_programmes"] or 0) if c else 0,
                }
            )
        return out

    def now_playing(self, items: list[dict]) -> None:
        """Set item["now"] = {title, start, stop} (or None) for dicts with source_id and
        epg_id: the programme on air right now in that guide's stored 48-hour schedule."""
        now = int(time.time())
        for it in items:
            row = self.db.one(
                "SELECT title, start, stop FROM epg_schedule WHERE source_id = ? AND epg_id_lc = ?"
                " AND start <= ? AND stop > ? ORDER BY start DESC LIMIT 1",
                (it.get("source_id"), xmltv.norm_id(it.get("epg_id") or ""), now, now),
            )
            it["now"] = dict(row) if row else None

    def diagnose(
        self, channels: list[Channel], mappings: dict[str, str] | None = None
    ) -> dict[str, dict]:
        """Explain why channels have no guide: what each source holds for the ids
        tried, and guide channels whose name resembles the channel (never applied
        automatically, only offered as mapping suggestions)."""
        if not channels:
            return {}
        mappings = mappings or {}
        not_before, not_after = self._epg_window()
        rows = self.db.query(
            """SELECT e.source_id, e.epg_id, e.epg_id_lc, e.display_name, e.programmes, e.first_start,
                      e.last_stop, s.name AS source
               FROM epg_channels e JOIN sources s ON s.id = e.source_id
               WHERE s.enabled = 1 AND s.kind = 'xmltv' ORDER BY s.priority, s.id"""
        )
        by_id: dict[str, list] = {}
        for r in rows:
            by_id.setdefault(r["epg_id_lc"], []).append(r)
        index = None
        out = {}
        for ch in channels:
            mapped = mappings.get(ch.key)
            cands = [xmltv.norm_id(mapped)] if mapped else ch.candidates()
            found = [
                {
                    "source": r["source"],
                    "epg_id": r["epg_id"],
                    "programmes": r["programmes"],
                    "usable": _in_window(r, not_before, not_after),
                }
                for c in cands
                for r in by_id.get(c, [])
            ]
            suggestions = []
            if not any(f["usable"] for f in found):
                if index is None:
                    index = [
                        (
                            r,
                            _name_tokens(
                                r["display_name"] or r["epg_id"], _id_country(r["epg_id"])
                            ),
                        )
                        for r in rows
                        if r["programmes"] > 0
                    ]
                suggestions = _suggest(ch, index)
            out[ch.key] = {"candidates": cands, "found": found, "suggestions": suggestions}
        return out

    # ============================================================ worker

    def start(self) -> None:
        # A missing feed file is built now so the first request finds it. Otherwise every
        # start queues a rebuild of both feeds from cached data (no downloads), so a new
        # version's output rules apply without a button; the worker runs it after the server
        # is up. A failed build keeps the old files.
        if not self.playlist_path.exists():
            self.build_playlist()
        else:
            self.request_playlist()
        if not self.epg_path.exists() or not self.epg_gz_path.exists():
            self.build_epg()
        else:
            self.request_epg()
        self.current_job = None  # the initial builds above leave their label behind
        self._thread = threading.Thread(target=self._run, name="worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def request_refresh(self, source_ids: list[int] | None = None) -> None:
        if source_ids is None:
            source_ids = [
                r["id"] for r in self.db.query("SELECT id FROM sources WHERE enabled = 1")
            ]
        with self._cond:
            self._pending_refresh.update(source_ids)
            self._cond.notify_all()

    def request_playlist(self) -> None:
        with self._cond:
            self._pending_playlist = True
            self._cond.notify_all()

    def request_epg(self, delay: float = 0, force_scan: bool = False) -> None:
        with self._cond:
            at = time.time() + delay
            # Debounce: a later request pushes a pending delayed build further out.
            if self._pending_epg_at is None or delay > 0 or at < self._pending_epg_at:
                self._pending_epg_at = at
            self._force_epg_scan |= force_scan
            self._cond.notify_all()

    def request_metadata(self) -> None:
        with self._cond:
            self._refresh_metadata = True
            self._cond.notify_all()

    def _next_scheduled(self) -> float | None:
        if self.cfg.refresh_seconds <= 0:
            return None
        last = self.db.get_kv("last_scheduled_refresh", 0) or 0
        return last + self.cfg.refresh_seconds

    def epg_pending(self) -> bool:
        """Is a guide build queued or running (so coverage may be about to change)?"""
        job = self.current_job or ""
        with self._cond:
            queued = self._pending_epg_at is not None
        return queued or job.startswith(("Filtering guide", "Building guide", "Writing guide"))

    def pending(self) -> dict:
        with self._cond:
            return {
                "refresh": sorted(self._pending_refresh),
                "playlist": self._pending_playlist,
                "epg_at": self._pending_epg_at,
            }

    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._stop:
                        return
                    now = time.time()
                    scheduled = self._next_scheduled()
                    if scheduled is not None and scheduled <= now:
                        self.db.set_kv("last_scheduled_refresh", now)
                        self._pending_refresh.update(
                            r["id"]
                            for r in self.db.query("SELECT id FROM sources WHERE enabled = 1")
                        )
                        self._refresh_metadata = True
                    if self._pending_refresh or self._pending_playlist or self._refresh_metadata:
                        break
                    if self._pending_epg_at is not None and self._pending_epg_at <= now:
                        break
                    deadlines = [t for t in (scheduled, self._pending_epg_at) if t is not None]
                    self._cond.wait(timeout=max(1.0, min(deadlines) - now) if deadlines else None)
                refresh_ids = self._pending_refresh
                self._pending_refresh = set()
                do_playlist = self._pending_playlist
                self._pending_playlist = False
                refresh_metadata = self._refresh_metadata
                self._refresh_metadata = False
                generation = self._generation
            with self.job_lock:
                with self._cond:
                    replaced = generation != self._generation
                if replaced:
                    continue  # an import swapped the data and queued everything again
                try:
                    self._execute(refresh_ids, do_playlist, refresh_metadata)
                except Exception:  # noqa: BLE001 - never let the worker die
                    log.exception("background job failed")
                finally:
                    self.current_job = None

    def replace_all(self, write: Callable[[sqlite3.Connection], None], sources: list[int]) -> None:
        """Replaces the admin's data: `write` runs in one transaction on a database it may
        empty; then cache files of every source id go, whatever was pending is dropped, and
        a full refresh of `sources`, the playlist and the guide is queued as one job (a new id
        can equal an old one, so nothing cached may be trusted). Raises Busy, changing
        nothing, while the worker runs a job."""
        if not self.job_lock.acquire(blocking=False):
            raise Busy()
        try:
            self.current_job = "Importing backup"
            with self.db.conn() as c:
                write(c)
            try:
                for p in self.cfg.cache_dir.iterdir():
                    if re.match(r"(?:xmltv|m3u)-\d+\.", p.name):
                        try:
                            p.unlink(missing_ok=True)
                        except OSError as exc:  # the refresh replaces it; never leave a half import
                            log.warning("import: could not remove a cache file: %s", exc)
            finally:  # the data is already replaced: the rest must happen whatever went wrong
                self.invalidate_catalog()
                with self._cond:
                    self._generation += 1
                    self._pending_refresh = set(sources)
                    self._pending_playlist = True
                    self._pending_epg_at = time.time()
                    self._force_epg_scan = True
                    self._refresh_metadata = False
                    self._cond.notify_all()
        finally:
            self.current_job = None
            self.job_lock.release()

    def _execute(self, refresh_ids: set[int], do_playlist: bool, refresh_metadata: bool) -> None:
        m3u_changed = xmltv_changed = False
        if refresh_ids:
            placeholders = ",".join("?" * len(refresh_ids))
            sources = self.db.query(
                f"SELECT * FROM sources WHERE id IN ({placeholders}) ORDER BY kind, priority, id",
                list(refresh_ids),
            )
            for src in sources:
                if src["kind"] != "m3u":
                    continue
                self.current_job = f"Refreshing {src['name']}"
                m3u_changed |= self.refresh_m3u(src)
            for src in sources:
                if src["kind"] != "xmltv":
                    continue
                self.current_job = f"Refreshing {src['name']}"
                try:
                    xmltv_changed |= self.refresh_xmltv(src)
                finally:
                    self._cleanup_tmp(src["id"])
        if m3u_changed or refresh_metadata:
            self.current_job = "Refreshing language metadata"
            self.refresh_languages()
            do_playlist = True
        if do_playlist or m3u_changed:
            self.current_job = "Building playlist"
            self.build_playlist()
        with self._cond:
            epg_due = self._pending_epg_at is not None and self._pending_epg_at <= time.time()
            force = self._force_epg_scan
            if epg_due or m3u_changed or xmltv_changed:
                self._pending_epg_at = None
                self._force_epg_scan = False
        if epg_due or m3u_changed or xmltv_changed:
            self.current_job = "Building guide"
            self.build_epg(force_scan=force)
        self._sweep_cache()

    def _sweep_cache(self) -> None:
        """Remove cache files of sources deleted while a job was running."""
        ids = {r["id"] for r in self.db.query("SELECT id FROM sources")}
        for p in self.cfg.cache_dir.iterdir():
            m = re.match(r"(?:xmltv|m3u)-(\d+)\.", p.name)
            if m and int(m.group(1)) not in ids:
                p.unlink(missing_ok=True)


def _in_window(row, not_before: int, not_after: int | None) -> bool:
    """Does an epg_channels row have programmes the output window would keep?"""
    if not row["programmes"] or row["last_stop"] is None:
        return False
    return row["last_stop"] >= not_before and (not_after is None or row["first_start"] <= not_after)


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TOKEN_ALIASES = {
    "televize": "tv",
    "television": "tv",
    "televizia": "tv",
    "televizija": "tv",
    "telewizja": "tv",
    "fernsehen": "tv",
    "and": "&",
    "a": "&",
}
_TOKEN_NOISE = {"hd", "sd", "fhd", "uhd", "4k", "hevc", "channel", "&"}


def _id_country(epg_id: str) -> str:
    base, _ = m3u.split_tvg_id(epg_id)
    tail = base.rsplit(".", 1)[-1] if "." in base else ""
    return tail.lower() if len(tail) == 2 and tail.isalpha() else ""


def _name_tokens(text: str, *drop: str) -> frozenset[str]:
    out = set()
    for tok in _TOKEN_RE.findall(m3u.fold(text)):
        tok = _TOKEN_ALIASES.get(tok, tok)
        if tok not in _TOKEN_NOISE and tok not in drop:
            out.add(tok)
    return frozenset(out)


def _token_hits(a: frozenset[str], b: frozenset[str]) -> int:
    """Tokens of a found in b, allowing word abbreviations ("nat" ~ "national").

    Only purely alphabetic tokens may abbreviate, so "ct24" never matches "ct2".
    """

    def abbrev(x: str, y: str) -> bool:
        short, long_ = sorted((x, y), key=len)
        return len(short) >= 3 and short.isalpha() and long_.isalpha() and long_.startswith(short)

    return sum(1 for x in a if x in b or any(abbrev(x, y) for y in b))


def _suggest(ch: Channel, index: list, limit: int = 3) -> list[dict]:
    countries = [c.lower() for c in ch.countries]
    mine = _name_tokens(ch.name, *countries)
    if not mine:
        return []
    scored = []
    for r, theirs in index:
        if not theirs:
            continue
        score = min(_token_hits(mine, theirs) / len(mine), _token_hits(theirs, mine) / len(theirs))
        if score >= 0.6:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])  # stable: keeps source priority order among equals
    out, seen = [], set()
    for score, r in scored:
        if r["epg_id_lc"] in seen:
            continue
        seen.add(r["epg_id_lc"])
        out.append(
            {
                "epg_id": r["epg_id"],
                "display_name": r["display_name"],
                "programmes": r["programmes"],
                "source": r["source"],
                "source_id": r["source_id"],
                "score": round(score, 2),
            }
        )
        if len(out) == limit:
            break
    return out


def parse_feed_languages(read: Callable[[int], str], wanted: set[str]) -> dict[str, list[str]]:
    """Stream-decode iptv-org's feeds.json (one big JSON array), keeping only what we need.

    ``read(n)`` returns the next chunk of text ("" at EOF). Objects are decoded one
    at a time from a small sliding buffer, so memory stays flat regardless of file size.
    """
    decoder = json.JSONDecoder()
    buf, pos, eof, started = "", 0, False, False
    out: dict[str, list[str]] = {}

    def more() -> None:
        nonlocal buf, pos, eof
        chunk = read(256 * 1024)
        if not chunk:
            eof = True
        buf, pos = buf[pos:] + chunk, 0
        if len(buf) > MAX_JSON_OBJECT:
            raise ValueError("JSON object too large")

    while True:
        while True:  # skip separators, refilling as needed
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf) or eof:
                break
            more()
        if pos >= len(buf):
            raise ValueError("unexpected end of JSON")
        if not started:
            if buf[pos] != "[":
                raise ValueError("expected a JSON array")
            started, pos = True, pos + 1
            continue
        if buf[pos] == "]":
            return out
        try:
            obj, pos = decoder.raw_decode(buf, pos)
        except json.JSONDecodeError:
            if eof:
                raise ValueError("malformed JSON") from None
            more()  # object straddles the chunk boundary
            continue
        if not isinstance(obj, dict):
            continue
        channel = str(obj.get("channel") or "")
        langs = [str(lang) for lang in obj.get("languages") or [] if lang]
        if not channel or not langs:
            continue
        feed_key = f"{channel}@{obj.get('id', '')}".lower()
        if feed_key in wanted:
            out[feed_key] = langs
        if obj.get("is_main") and channel.lower() in wanted:
            out.setdefault(channel.lower(), langs)
