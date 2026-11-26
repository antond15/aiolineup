"""Extended M3U parsing, channel identity and playlist rendering."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

# key="value" | key='value' | key=value
_ATTR_RE = re.compile(r"""([A-Za-z0-9_][\w.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"',]+))""")
# Status / quality tags iptv-org appends to names: "(1080p)", "[Geo-blocked]", "[Not 24/7]"
_TAG_RE = re.compile(r"\s*(\(\s*\d{3,4}[pi]\s*\)|\[[^\]]*\])\s*", re.I)
_WS_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Directive lines that belong to the following stream entry.
_DIRECTIVE_PREFIXES = ("#EXTVLCOPT", "#KODIPROP", "#EXTHTTP", "#EXTGRP")


@dataclass
class Entry:
    """One stream line from a playlist, with everything needed to re-emit it."""

    title: str
    url: str
    duration: str = "-1"
    attrs: dict[str, str] = field(default_factory=dict)  # original order, original key case
    directives: list[str] = field(default_factory=list)

    def attr(self, name: str) -> str:
        name = name.lower()
        for key, value in self.attrs.items():
            if key.lower() == name:
                return value.strip()
        return ""

    @property
    def tvg_id(self) -> str:
        return self.attr("tvg-id")

    @property
    def group(self) -> str:
        group = self.attr("group-title")
        if not group:
            for line in self.directives:
                if line.upper().startswith("#EXTGRP:"):
                    return line.split(":", 1)[1].strip()
        return group


@dataclass
class ParseStats:
    """Counters a streaming parse fills in as its entries are consumed."""

    skipped: int = 0  # malformed lines / stream URLs without #EXTINF
    had_header: bool = False


@dataclass
class ParseResult:
    entries: list[Entry]
    skipped: int
    had_header: bool


def _split_extinf(body: str) -> tuple[str, str]:
    """Split '<duration> <attrs>,<title>' at the first comma outside quotes."""
    quote = ""
    for i, ch in enumerate(body):
        if ch in "\"'" and (not quote or quote == ch):
            quote = "" if quote else ch
        elif ch == "," and not quote:
            return body[:i], body[i + 1 :]
    return body, ""


def parse_extinf(line: str) -> tuple[str, dict[str, str], str]:
    body = line[len("#EXTINF:") :].strip()
    head, title = _split_extinf(body)
    head = head.strip()
    duration_match = re.match(r"^-?\d+(?:\.\d+)?", head)
    duration = duration_match.group(0) if duration_match else "-1"
    attrs: dict[str, str] = {}
    for m in _ATTR_RE.finditer(head[duration_match.end() if duration_match else 0 :]):
        value = (
            m.group(2)
            if m.group(2) is not None
            else m.group(3)
            if m.group(3) is not None
            else m.group(4)
        )
        attrs.setdefault(m.group(1), _CTRL_RE.sub("", value))
    return duration, attrs, _CTRL_RE.sub("", title).strip()


def _ci(attrs: dict[str, str], name: str) -> str:
    for key, value in attrs.items():
        if key.lower() == name:
            return value.strip()
    return ""


def parse(lines: Iterable[str], stats: ParseStats | None = None) -> Iterator[Entry]:
    """Yield the entries of a playlist one at a time, so a large file never sits in memory.

    ``stats`` (if given) is updated while the generator runs and is final once it is exhausted.
    """
    stats = stats if stats is not None else ParseStats()
    pending: tuple[str, dict[str, str], str] | None = None
    directives: list[str] = []
    first = True

    for raw in lines:
        if first:
            first = False
            raw = raw.removeprefix("\ufeff")
        line = raw.strip()
        if not line:
            continue
        upper = line[:12].upper()
        if upper.startswith("#EXTM3U"):
            stats.had_header = True
            continue
        if upper.startswith("#EXTINF:"):
            if pending is not None:
                stats.skipped += 1  # previous EXTINF never got a URL
            try:
                pending = parse_extinf(line)
            except Exception:  # noqa: BLE001 - malformed line, keep going
                pending = None
                stats.skipped += 1
            directives = []
            continue
        if line.startswith("#"):
            if pending is not None and upper.startswith(_DIRECTIVE_PREFIXES):
                directives.append(_CTRL_RE.sub("", line))
            continue
        # A stream URL line.
        if pending is None or _CTRL_RE.search(line):
            stats.skipped += 1
            pending = None
            directives = []
            continue
        duration, attrs, title = pending
        name = title or _ci(attrs, "tvg-name")
        if not name and not _ci(attrs, "tvg-id"):
            stats.skipped += 1
        else:
            yield Entry(title=name, url=line, duration=duration, attrs=attrs, directives=directives)
        pending = None
        directives = []

    if pending is not None:
        stats.skipped += 1


def parse_text(text: str) -> ParseResult:
    """Parse a whole playlist held as text (tests, small inputs)."""
    stats = ParseStats()
    entries = list(parse(text.splitlines(), stats))
    return ParseResult(entries=entries, skipped=stats.skipped, had_header=stats.had_header)


# ---------------------------------------------------------------- identity


def clean_name(name: str) -> str:
    """Display name without iptv-org quality/status tags."""
    cleaned = _WS_RE.sub(" ", _TAG_RE.sub(" ", name)).strip()
    return cleaned or name.strip()


def channel_name(entry: Entry) -> str:
    """The name a channel's first stream gives it in the catalogue."""
    return clean_name(entry.title or entry.attr("tvg-name") or entry.tvg_id)


def fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", value).strip().casefold()


def channel_key(entry: Entry) -> str:
    """Stable identity used for selections.

    With a tvg-id the id is authoritative (case-insensitive). Without one we fall
    back to the exact cleaned name: identical names across playlists are treated
    as the same channel, but similar names never are.
    """
    tvg_id = entry.tvg_id
    if tvg_id:
        return "id:" + fold(tvg_id)
    return "name:" + fold(clean_name(entry.title or entry.attr("tvg-name")))


def split_tvg_id(tvg_id: str) -> tuple[str, str]:
    """iptv-org ids look like 'RedBullTV.at@SD' -> ('RedBullTV.at', 'SD')."""
    base, _, feed = tvg_id.partition("@")
    return base, feed


def country_from_id(tvg_id: str) -> str:
    base, _ = split_tvg_id(tvg_id)
    if "." in base:
        suffix = base.rsplit(".", 1)[1]
        if len(suffix) == 2 and suffix.isalpha():
            return suffix.upper()
    return ""


def categories(group: str) -> list[str]:
    out = []
    for part in re.split(r"[;|]", group or ""):
        part = part.strip()
        if part and part.lower() not in ("undefined", "other") and part not in out:
            out.append(part)
    return out


# ---------------------------------------------------------------- rendering


def attr_value(value: str) -> str:
    """An attribute value as render_entry writes it (control characters out, `"` as `'`)."""
    return _CTRL_RE.sub("", value).replace('"', "'")


def render_entry(
    title: str,
    url: str,
    attrs: dict[str, str],
    directives: list[str],
    duration: str = "-1",
) -> str:
    parts = [f"#EXTINF:{duration}"]
    for key, value in attrs.items():
        parts.append(f' {key}="{attr_value(value)}"')
    title = _CTRL_RE.sub("", title).strip()
    lines = ["".join(parts) + "," + title]
    lines.extend(_CTRL_RE.sub("", d) for d in directives)
    lines.append(_CTRL_RE.sub("", url))
    return "\n".join(lines) + "\n"


def stream_signature(url: str, directives: list[str]) -> tuple[str, tuple[str, ...]]:
    """Two entries are duplicates only if URL *and* request directives match.

    #EXTGRP is a category, not a request directive, and the feed drops it, so it is ignored.
    """
    return url.strip(), tuple(
        sorted(d.strip() for d in directives if not d.upper().startswith("#EXTGRP"))
    )


def signature_key(url: str, directives: list[str]) -> str:
    """stream_signature as one text value: the stored and API identity of a stream."""
    url, directives = stream_signature(url, directives)
    return json.dumps([url, list(directives)], ensure_ascii=False, separators=(",", ":"))
