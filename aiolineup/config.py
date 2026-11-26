"""Environment configuration. Everything has a sensible default except the admin password."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

MiB = 1024 * 1024


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise SystemExit(f"{name} must be >= {minimum}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str
    port: int
    admin_user: str
    admin_password: str
    feed_token: str
    public_url: str
    refresh_seconds: int
    request_timeout: int
    m3u_max_bytes: int
    epg_max_download_bytes: int
    epg_max_decompressed_bytes: int
    allow_private_sources: bool
    secure_cookies: bool | None  # None = decide from X-Forwarded-Proto

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aiolineup.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"


def _persistent_secret(path: Path, nbytes: int = 32) -> str:
    """Read a generated secret from the data volume, creating it on first start."""
    try:
        value = path.read_text().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_hex(nbytes)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)
    os.replace(tmp, path)
    return value


def load() -> Config:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    password = os.environ.get("ADMIN_PASSWORD", "")
    if len(password) < 8:
        raise SystemExit("ADMIN_PASSWORD must be set (at least 8 characters).")

    token = os.environ.get("FEED_TOKEN", "").strip()
    if not token:
        token = _persistent_secret(data_dir / "feed_token")
    if len(token) < 16 or not token.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("FEED_TOKEN must be at least 16 characters of [A-Za-z0-9_-].")

    secure = os.environ.get("SECURE_COOKIES", "").strip().lower()

    return Config(
        data_dir=data_dir,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=_int("PORT", 8080, 0),
        admin_user=os.environ.get("ADMIN_USER", "admin") or "admin",
        admin_password=password,
        feed_token=token,
        public_url=os.environ.get("PUBLIC_URL", "").strip().rstrip("/"),
        refresh_seconds=_int("REFRESH_SECONDS", 21600, 0),
        request_timeout=_int("REQUEST_TIMEOUT", 60, 1),
        m3u_max_bytes=_int("M3U_MAX_MB", 20, 1) * MiB,
        epg_max_download_bytes=_int("EPG_MAX_DOWNLOAD_MB", 200, 1) * MiB,
        epg_max_decompressed_bytes=_int("EPG_MAX_DECOMPRESSED_MB", 1024, 1) * MiB,
        allow_private_sources=_bool("ALLOW_PRIVATE_SOURCES", False),
        secure_cookies=None if secure in {"", "auto"} else secure in {"1", "true", "yes", "on"},
    )
