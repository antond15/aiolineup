"""Input rules the admin API and the backup import share. Each function returns the cleaned
value or raises ValueError with the message the API shows."""

from __future__ import annotations

import re
from typing import Any

from . import fetch
from .db import DEFAULT_SETTINGS
from .engine import MAX_CATEGORIES

# Characters an own name or logo may not hold: controls (the playlist strips them, so the feeds
# would differ) and what XML 1.0 forbids (one would make the whole guide unreadable).
UNSAFE_TEXT_RE = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff\U0000fffe\U0000ffff]")


def source_fields(data: dict, partial: bool) -> dict:
    """name, url, kind, enabled and priority of a source as the API takes them; with
    `partial` a field left out is left out of the result."""
    fields: dict[str, Any] = {}
    if "name" in data or not partial:
        name = str(data.get("name") or "").strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("Name must be 1-100 characters.")
        fields["name"] = name
    if "url" in data or not partial:
        fields["url"] = fetch.validate_url(str(data.get("url") or ""))
    if "kind" in data or not partial:
        if data.get("kind") not in ("m3u", "xmltv"):
            raise ValueError("Kind must be m3u or xmltv.")
        fields["kind"] = data["kind"]
    if "enabled" in data:
        fields["enabled"] = 1 if data["enabled"] else 0
    if "priority" in data:
        try:
            fields["priority"] = max(0, min(10000, int(data["priority"])))
        except (TypeError, ValueError):
            raise ValueError("Priority must be a number.") from None
    return fields


def categories(value: Any) -> list[str]:
    """A channel's own category list (1 to MAX_CATEGORIES items)."""
    if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
        raise ValueError("Categories must be a list of text, or null.")
    value = [c.strip() for c in value]
    if not 1 <= len(value) <= MAX_CATEGORIES:
        raise ValueError(f"A channel takes 1 to {MAX_CATEGORIES} categories.")
    for category in value:
        if not 1 <= len(category) <= 60:
            raise ValueError("Each category must be 1-60 characters.")
        # ; and | split a category in the feed and the source parser; a quote would
        # reach the feed as ' (m3u.attr_value), so the app and feed would differ.
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in category) or re.search(r'[;|"]', category):
            raise ValueError('A category cannot contain ; | " or control characters.')
    return value


def edit_name(value: Any) -> str:
    """An own channel name; "" resets it."""
    if value is not None and not isinstance(value, str):
        raise ValueError("Name must be text or null.")
    name = (value or "").strip()
    if len(name) > 200 or UNSAFE_TEXT_RE.search(name):
        raise ValueError("Name must be at most 200 characters, without control characters.")
    return name


def edit_logo(value: Any, *, https_only: bool = True) -> str:
    """An own channel logo URL; "" resets it. https only unless ``https_only`` is off (a
    backup may carry an older http one)."""
    if value is not None and not isinstance(value, str):
        raise ValueError("Logo must be a URL or null.")
    logo = (value or "").strip()
    if logo:
        try:
            fetch.validate_url(logo)
        except ValueError as exc:
            raise ValueError(f"Logo: {exc}") from None
        if https_only and not logo.lower().startswith("https://"):
            raise ValueError("Logo must be an https:// URL.")
        if '"' in logo:  # the playlist writes " as ', so the feed would differ
            raise ValueError('Logo URL cannot contain ".')
        if UNSAFE_TEXT_RE.search(logo):
            raise ValueError("Logo URL contains a character feeds cannot carry.")
    return logo


def epg_id(value: Any) -> str:
    """A guide id of a manual mapping; "" means no guide for the channel."""
    text = str(value).strip()
    if len(text) > 300 or any(ord(ch) < 32 for ch in text):
        raise ValueError("Invalid guide id.")
    return text


def setting(key: str, value: Any) -> Any:
    """One setting's cleaned value; an unknown key is refused."""
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"Unknown setting {key}.")
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int):
        try:
            return max(0, min(168 if key == "epg_past_hours" else 60, int(value)))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number.") from None
    try:
        return fetch.validate_url(str(value))
    except ValueError as exc:
        raise ValueError(f"{key}: {exc}") from None
