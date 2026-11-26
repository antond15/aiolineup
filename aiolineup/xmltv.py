"""Streaming XMLTV filtering.

Two stages, both constant-memory with respect to the upstream guide size:

1. ``scan``: one incremental pass over a (possibly gzipped) upstream guide.
   Builds a small channel index (id, display name, programme count) and copies
   the ``<programme>`` elements of *candidate* channel ids into a small per-source
   fragment file. Every other element is discarded as soon as it is parsed.
2. ``assemble``: reads the small fragments in priority order, picks one guide
   source per selected channel, rewrites channel ids to the playlist's tvg-id,
   applies the time window, drops duplicate programmes and writes the final
   guide (plain + gzip) atomically.

Parsing uses the stdlib expat-based ElementTree: no DTD/external entity
loading, and expat >= 2.4 guards against entity-expansion bombs. Decompressed
input size is capped separately (see fetch.BoundedReader).
"""

from __future__ import annotations

import calendar
import gzip
import html
import json
import os
import re
import shutil
import time
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .fetch import open_maybe_gzip


class GuideError(Exception):
    pass


# Bump when norm_id() changes so cached per-source indexes are rebuilt.
NORM_VERSION = 2

_CHARREF_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")


def display_id(value: str) -> str:
    """Channel id as published, with leftover character references decoded.

    Some providers (e.g. free-epg.de) double-escape ids, so after XML parsing
    the id is the literal text ``&#x10C;T1.cz`` instead of ``ČT1.cz``.
    """
    value = value.strip()
    if "&" in value:
        value = _CHARREF_RE.sub(lambda m: html.unescape(m.group(0)), value)
    return value


def norm_id(value: str) -> str:
    """Matching key for a channel id: decoded, accents removed, lowercase.

    Applied identically to playlist candidates, manual mappings, guide channel
    declarations and programme references, so "ČT1.cz", "&#x10C;T1.cz" and
    "CT1.cz" are the same channel.
    """
    value = display_id(value)
    if not value.isascii():
        value = "".join(
            ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
        )
    return value.lower()


@dataclass
class IndexEntry:
    epg_id: str
    display_name: str = ""
    programmes: int = 0
    # Time span of the programmes (unix seconds); tracked for wanted ids only,
    # so routing can skip a source whose data is entirely outside the window.
    first_start: int | None = None
    last_stop: int | None = None


@dataclass
class ScanResult:
    index: dict[str, IndexEntry] = field(default_factory=dict)  # by lowercase id
    kept: int = 0
    decompressed_bytes: int = 0


def _iter_top_level(reader):
    """Yield each direct child of the root element, then free it."""
    try:
        ctx = ET.iterparse(reader, events=("start", "end"))
        _, root = next(ctx)
    except StopIteration:
        raise GuideError("Guide is empty.") from None
    except ET.ParseError as exc:
        raise GuideError(f"Not a valid XMLTV document: {exc}") from None
    if root.tag != "tv":
        raise GuideError(f"Not an XMLTV document (root element is <{root.tag}>).")
    depth = 1
    try:
        for event, elem in ctx:
            if event == "start":
                depth += 1
                continue
            depth -= 1
            if depth == 1:
                yield elem
                root.clear()
    except ET.ParseError as exc:
        raise GuideError(f"Malformed XML in guide: {exc}") from None


SCHEDULE_HOURS = 48


def scan(
    raw_path: Path,
    wanted: set[str],
    fragment_path: Path,
    max_decompressed: int,
    schedule_path: Path | None = None,
    now: int | None = None,
) -> ScanResult:
    """Filter one guide: write the wanted programmes to ``fragment_path`` and index
    every channel. With ``schedule_path``, also write every channel's programmes from
    ``now`` to SCHEDULE_HOURS later as JSON lines ([id, start, stop, title]), streamed
    to disk so memory stays flat."""
    result = ScanResult()
    index = result.index
    if now is None:
        now = int(time.time())
    horizon = now + SCHEDULE_HOURS * 3600
    fh, reader = open_maybe_gzip(raw_path, max_decompressed)
    sched = None
    try:
        if schedule_path is not None:
            sched = open(schedule_path, "w", encoding="utf-8", newline="\n")
        with open(fragment_path, "w", encoding="utf-8") as out:
            out.write("<tv>\n")
            for elem in _iter_top_level(reader):
                if elem.tag == "programme":
                    cid = (elem.get("channel") or "").strip()
                    if not cid:
                        continue
                    lc = norm_id(cid)
                    entry = index.get(lc)
                    if entry is None:
                        entry = index[lc] = IndexEntry(display_id(cid))
                    entry.programmes += 1
                    start = parse_time(elem.get("start"))
                    stop = None
                    if start is not None:
                        stop = parse_time(elem.get("stop")) or start
                        # The span of every id, wanted or not, so a diagnosis can tell "no
                        # programmes now" from "not in the guide".
                        if entry.first_start is None or start < entry.first_start:
                            entry.first_start = start
                        if entry.last_stop is None or stop > entry.last_stop:
                            entry.last_stop = stop
                        if sched is not None and start < horizon and stop > now:
                            # Cut before collapsing whitespace so a huge title stays cheap.
                            title = " ".join((elem.findtext("title") or "")[:400].split())
                            sched.write(json.dumps([lc, start, stop, title[:200]]) + "\n")
                    if lc in wanted:
                        elem.tail = "\n"
                        out.write(ET.tostring(elem, encoding="unicode"))
                        result.kept += 1
                elif elem.tag == "channel":
                    cid = (elem.get("id") or "").strip()
                    if not cid:
                        continue
                    entry = index.setdefault(norm_id(cid), IndexEntry(display_id(cid)))
                    if not entry.display_name:
                        entry.display_name = (elem.findtext("display-name") or "").strip()[:200]
            out.write("</tv>\n")
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise GuideError(f"Could not read guide: {exc}") from None
    finally:
        fh.close()
        if sched is not None:
            sched.close()
    result.decompressed_bytes = reader.count
    if not index:
        raise GuideError("Guide contains no channels or programmes.")
    return result


# ------------------------------------------------------------------ assembly

_TIME_RE = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?\s*([+-]\d{2}:?\d{2})?")


def parse_time(value: str | None) -> int | None:
    """XMLTV timestamp ('20261124153000 +0100') -> unix seconds, or None."""
    if not value:
        return None
    m = _TIME_RE.match(value)
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    try:
        ts = calendar.timegm(
            (int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), 0, 0, 0)
        )
    except (ValueError, OverflowError):
        return None
    if tz:
        tz = tz.replace(":", "")
        offset = int(tz[1:3]) * 3600 + int(tz[3:5]) * 60
        ts -= offset if tz[0] == "+" else -offset
    return ts


# JavaScript's \s (what AIOStreams' nameKey uses) differs from Python's: it has U+FEFF and
# lacks \x1c-\x1f and U+0085.
_NAME_RUN = re.compile(
    r"[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff_]+"
)


def name_key(name: str) -> str:
    """A name as AIOStreams matches it: lowercase, whitespace and ``_`` runs as one space."""
    return _NAME_RUN.sub(" ", name.lower()).strip(" ")


def lookup_key(name: str) -> str:
    """The key AIOStreams looks a playlist title up by: the title cut to 256 UTF-16 units."""
    cut = name.encode("utf-16-le")[:512].decode("utf-16-le", errors="ignore")
    return name_key(cut)


@dataclass
class OutputChannel:
    out_id: str
    name: str
    logo: str = ""


@dataclass
class AssembleStats:
    channels: int = 0
    programmes: int = 0
    duplicates: int = 0
    outside_window: int = 0
    per_channel: dict[str, int] = field(default_factory=dict)  # out_id -> programmes written
    size_bytes: int = 0
    gz_size_bytes: int = 0


def assemble(
    fragments: list[tuple[int, Path]],
    routes: dict[tuple[int, str], list[str]],
    channels: list[OutputChannel],
    out_xml: Path,
    out_gz: Path,
    *,
    not_before: int | None,
    not_after: int | None,
) -> AssembleStats:
    """Write the final guide. ``routes`` maps (source_id, norm_id(epg_id)) -> output channel ids."""
    stats = AssembleStats()
    tmp_xml = out_xml.with_name(out_xml.name + ".tmp")
    tmp_gz = out_gz.with_name(out_gz.name + ".tmp")
    seen: set[tuple[str, str]] = set()
    try:
        with open(tmp_xml, "w", encoding="utf-8") as out:
            out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            out.write('<tv generator-info-name="aiolineup">\n')
            # Every selected channel is listed, also without a guide: valid XMLTV, and other
            # players build their channel list from it. AIOStreams falls back to matching a
            # channel's name against display names, so names that collide get the id
            # appended: a channel without programmes then never takes a namesake's.
            lookups: dict[str, int] = {}
            for ch in channels:
                k = lookup_key(ch.name)
                lookups[k] = lookups.get(k, 0) + 1
            used: set[str] = set()
            for ch in channels:
                el = ET.Element("channel", id=ch.out_id)
                shown = ch.name
                # Unique among display names and among the other channels' lookup keys; a
                # name past 256 characters matches nothing, so it needs no change.
                n = 0
                while len(name_key(shown)) <= 256 and (
                    name_key(shown) in used
                    or lookups.get(name_key(shown), 0)
                    > (1 if lookup_key(ch.name) == name_key(shown) else 0)
                ):
                    n += 1
                    shown = f"{ch.name} ({ch.out_id})" if n == 1 else f"{ch.name} ({ch.out_id}) {n}"
                used.add(name_key(shown))
                ET.SubElement(el, "display-name").text = shown
                if ch.logo:
                    ET.SubElement(el, "icon", src=ch.logo)
                el.tail = "\n"
                out.write(ET.tostring(el, encoding="unicode"))
                stats.channels += 1
            for source_id, path in fragments:
                if not path.exists():
                    continue
                with open(path, "rb") as fh:
                    for elem in _iter_top_level(fh):
                        if elem.tag != "programme":
                            continue
                        targets = routes.get((source_id, norm_id(elem.get("channel") or "")))
                        if not targets:
                            continue
                        start = elem.get("start") or ""
                        start_ts = parse_time(start)
                        stop_ts = parse_time(elem.get("stop"))
                        if start_ts is None:
                            continue
                        if not_before is not None and (stop_ts or start_ts) < not_before:
                            stats.outside_window += 1
                            continue
                        if not_after is not None and start_ts > not_after:
                            stats.outside_window += 1
                            continue
                        elem.tail = "\n"
                        for out_id in targets:
                            key = (out_id, str(start_ts))
                            if key in seen:
                                stats.duplicates += 1
                                continue
                            seen.add(key)
                            elem.set("channel", out_id)
                            out.write(ET.tostring(elem, encoding="unicode"))
                            stats.programmes += 1
                            stats.per_channel[out_id] = stats.per_channel.get(out_id, 0) + 1
            out.write("</tv>\n")
        with open(tmp_xml, "rb") as src, gzip.open(tmp_gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 256 * 1024)
        stats.size_bytes = tmp_xml.stat().st_size
        stats.gz_size_bytes = tmp_gz.stat().st_size
        os.replace(tmp_xml, out_xml)
        os.replace(tmp_gz, out_gz)
    finally:
        for p in (tmp_xml, tmp_gz):
            p.unlink(missing_ok=True)
    return stats
