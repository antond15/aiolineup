"""HTTP server: token-protected feeds, session-protected admin API, static UI."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, backup, m3u, rules, xmltv
from .config import Config, _persistent_secret
from .engine import MAX_CATEGORIES, MAX_NUMBER, Busy, Engine, feed_warnings

log = logging.getLogger("aiolineup.http")

STATIC_DIR = Path(__file__).parent / "static"


def _version_assets(html: bytes) -> bytes:
    """Add ?v=<content hash> to the UI's asset URLs. A proxy in front may cache static files for
    hours despite no-cache, and a new page with an old stylesheet breaks; a new hash forces a refetch."""
    digest = hashlib.sha256()
    for name in ("app.css", "app.js", "icons.svg", "icon.svg"):
        digest.update((STATIC_DIR / name).read_bytes())
    version = digest.hexdigest()[:10].encode()
    return re.sub(rb'(/static/[a-z0-9_-]+\.(?:css|js|svg))"', rb"\1?v=" + version + b'"', html)


SESSION_COOKIE = "aiolineup_session"
SESSION_TTL = 30 * 86400
MAX_BODY = 1024 * 1024
MAX_CONNECTIONS = 32
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILURES = 10
BULK_LIMIT = 2000
_FEED_RE = re.compile(r"^/feed/([^/]+)/(playlist\.m3u|epg\.xml|epg\.xml\.gz)$")
_REDACT_RE = re.compile(r"/feed/[^/\s?]+", re.IGNORECASE)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
CSP = (
    "default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class App:
    def __init__(self, cfg: Config, engine: Engine):
        self.cfg = cfg
        self.engine = engine
        self.db = engine.db
        secret = _persistent_secret(cfg.data_dir / "session_secret")
        # Changing ADMIN_PASSWORD invalidates all existing sessions.
        self.session_key = hmac.new(
            secret.encode(), cfg.admin_password.encode(), hashlib.sha256
        ).digest()
        self._failures: deque[float] = deque()
        self._failures_lock = threading.Lock()

    # ------------------------------------------------------------ sessions

    def make_session(self) -> str:
        expiry = str(int(time.time()) + SESSION_TTL)
        sig = hmac.new(self.session_key, expiry.encode(), hashlib.sha256).digest()
        return expiry + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def check_session(self, value: str | None) -> bool:
        if not value or "." not in value:
            return False
        expiry, sig = value.split(".", 1)
        if not expiry.isdigit() or int(expiry) < time.time():
            return False
        expected = (
            base64.urlsafe_b64encode(
                hmac.new(self.session_key, expiry.encode(), hashlib.sha256).digest()
            )
            .decode()
            .rstrip("=")
        )
        return hmac.compare_digest(sig, expected)

    def check_login(self, user: str, password: str) -> bool:
        with self._failures_lock:
            now = time.time()
            while self._failures and self._failures[0] < now - LOGIN_WINDOW:
                self._failures.popleft()
            if len(self._failures) >= LOGIN_MAX_FAILURES:
                raise HTTPError(429, "Too many failed logins. Try again in a few minutes.")
        ok_user = hmac.compare_digest(user.encode(), self.cfg.admin_user.encode())
        ok_pass = hmac.compare_digest(password.encode(), self.cfg.admin_password.encode())
        if ok_user and ok_pass:
            return True
        with self._failures_lock:
            self._failures.append(time.time())
        time.sleep(0.5)
        return False

    def feed_urls(self, base: str) -> dict:
        prefix = f"{base}/feed/{self.cfg.feed_token}"
        return {
            "playlist": f"{prefix}/playlist.m3u",
            "epg": f"{prefix}/epg.xml",
            "epg_gz": f"{prefix}/epg.xml.gz",
        }


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app: App):
        super().__init__(addr, Handler)
        self.app = app
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # overloaded: drop instead of spawning threads
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = f"aiolineup/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30  # per socket operation; stops slow clients holding threads

    # ------------------------------------------------------------ plumbing

    @property
    def app(self) -> App:
        return self.server.app

    def finish(self):
        super().finish()
        self.app.db.close()  # this thread's SQLite connection ends with its connection

    def _redact(self, text: str) -> str:
        """Strip feed tokens and query strings, however the client encoded them."""
        for _ in range(3):  # undo double or triple percent-encoding too
            decoded = unquote(text, errors="replace")
            if decoded == text:
                break
            text = decoded
        text = _REDACT_RE.sub("/feed/***", text)
        text = text.replace(self.app.cfg.feed_token, "***")
        return re.sub(r"\?\S*", "", text)

    def log_message(self, fmt, *args):  # never log feed tokens or query strings
        log.info("%s %s", self.address_string(), self._redact(fmt % args))

    def _base_headers(self, status: int, ctype: str, length: int | None, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def send_json(self, data, status: int = 200, extra: dict | None = None):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        self._base_headers(
            status,
            "application/json; charset=utf-8",
            len(body),
            {"Cache-Control": "no-store", **(extra or {})},
        )
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error_json(self, status: int, message: str):
        self.send_json({"error": message}, status)

    def read_json(self, limit: int = MAX_BODY) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPError(415, "Expected application/json.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HTTPError(400, "Bad Content-Length.") from None
        if length < 0:  # rfile.read(-1) would read until the client hangs up
            raise HTTPError(400, "Bad Content-Length.")
        if length > limit:
            raise HTTPError(413, "Request body too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPError(400, "Invalid JSON.") from None
        if not isinstance(data, dict):
            raise HTTPError(400, "Expected a JSON object.")
        return data

    def cookie(self, name: str) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def is_https(self) -> bool:
        if self.app.cfg.secure_cookies is not None:
            return self.app.cfg.secure_cookies
        return (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip() == "https"

    def base_url(self) -> str:
        if self.app.cfg.public_url:
            return self.app.cfg.public_url
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        host = host.split(",")[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
            host = "localhost"
        return f"{'https' if self.is_https() else 'http'}://{host}"

    # ------------------------------------------------------------ dispatch

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        try:
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            if path == "/health":
                self.app.db.one("SELECT 1")
                return self.send_json({"status": "ok", "version": __version__})
            # Tokens are [A-Za-z0-9_-], so a real feed URL never needs encoding;
            # matching the raw path refuses encoded variants that dodge log redaction.
            m = _FEED_RE.match(parts.path)
            if m and method == "GET":
                return self.serve_feed(m.group(1), m.group(2))
            if path.startswith("/api/"):
                return self.api(method, path[4:], query)
            if method == "GET":
                return self.serve_static(path)
            raise HTTPError(404, "Not found.")
        except HTTPError as exc:
            self.send_error_json(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception(
                "request failed: %s %s",
                method,
                self._redact(self.path),
            )
            try:
                self.send_error_json(500, "Internal error.")
            except OSError:
                pass

    # ------------------------------------------------------------ feeds

    def serve_feed(self, token: str, name: str):
        if not hmac.compare_digest(token.encode(), self.app.cfg.feed_token.encode()):
            raise HTTPError(404, "Not found.")
        engine = self.app.engine
        extra = {"Cache-Control": "no-cache"}
        if name == "playlist.m3u":
            path, ctype = engine.playlist_path, "audio/x-mpegurl; charset=utf-8"
        elif name == "epg.xml.gz":
            path, ctype = engine.epg_gz_path, "application/gzip"
        else:
            ctype = "application/xml; charset=utf-8"
            path = engine.epg_path
            extra["Vary"] = "Accept-Encoding"
            if (
                "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
                and engine.epg_gz_path.exists()
            ):
                path = engine.epg_gz_path
                extra["Content-Encoding"] = "gzip"
        try:
            fh = open(path, "rb")  # open first: an atomic replace can't change what we send
        except FileNotFoundError:
            raise HTTPError(503, "Feed not generated yet.") from None
        with fh:
            st = os.fstat(fh.fileno())
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            extra["ETag"] = etag
            extra["Last-Modified"] = self.date_time_string(int(st.st_mtime))
            if self.headers.get("If-None-Match") == etag:
                self._base_headers(304, ctype, None, extra)
                return
            self._base_headers(200, ctype, st.st_size, extra)
            if self.command == "HEAD":
                return
            self.wfile.flush()
            try:
                self.connection.sendfile(fh)
            except (AttributeError, OSError):
                # socket.sendfile leaves the file at the offset it sent up to.
                while chunk := fh.read(256 * 1024):
                    self.wfile.write(chunk)

    # ------------------------------------------------------------ static

    def serve_static(self, path: str):
        if path in ("/", "/index.html"):
            target = STATIC_DIR / "index.html"
        else:
            name = path.removeprefix("/static/") if path.startswith("/static/") else ""
            if not re.fullmatch(r"[a-z0-9_-]+\.(js|css|svg|png)", name):
                raise HTTPError(404, "Not found.")
            target = STATIC_DIR / name
        if not target.is_file():
            raise HTTPError(404, "Not found.")
        body = target.read_bytes()
        if target.name == "index.html":
            body = _version_assets(body)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._base_headers(
            200, ctype, len(body), {"Content-Security-Policy": CSP, "Cache-Control": "no-cache"}
        )
        if self.command != "HEAD":
            self.wfile.write(body)

    # ------------------------------------------------------------ API

    def api(self, method: str, path: str, query: dict):
        if method != "GET":
            # CSRF guard: a custom header can't be sent cross-site without a CORS
            # preflight, which we never approve. SameSite=Strict cookies add a second layer.
            if self.headers.get("X-Requested-With") != "aiolineup":
                raise HTTPError(403, "Missing X-Requested-With header.")
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc not in (
                urlsplit(self.base_url()).netloc,
                (self.headers.get("Host") or "").strip(),
            ):
                raise HTTPError(403, "Cross-origin request refused.")

        if path == "/login" and method == "POST":
            return self.api_login()
        if not self.app.check_session(self.cookie(SESSION_COOKIE)):
            raise HTTPError(401, "Login required.")

        routes = {
            ("POST", "/logout"): self.api_logout,
            ("GET", "/overview"): self.api_overview,
            ("GET", "/channels"): lambda: self.api_channels(query),
            ("GET", "/channel"): lambda: self.api_channel(query),
            ("POST", "/selection"): self.api_selection,
            ("PUT", "/channel/number"): self.api_channel_number,
            ("PUT", "/channel/streams"): self.api_channel_streams,
            ("PUT", "/channel/edit"): self.api_channel_edit,
            ("GET", "/sources"): self.api_sources,
            ("POST", "/sources"): self.api_source_create,
            ("POST", "/refresh"): self.api_refresh_all,
            ("GET", "/epg"): self.api_epg,
            ("GET", "/epg/search"): lambda: self.api_epg_search(query),
            ("PUT", "/epg/mapping"): self.api_epg_mapping,
            ("POST", "/epg/rebuild"): self.api_epg_rebuild,
            ("GET", "/settings"): self.api_settings,
            ("PUT", "/settings"): self.api_settings_update,
            ("GET", "/backup"): self.api_backup,
            ("POST", "/backup/import"): lambda: self.api_backup_import(query),
        }
        handler = routes.get((method, path))
        if handler:
            return handler()
        m = re.fullmatch(r"/sources/(\d+)(/refresh)?", path)
        if m:
            sid = int(m.group(1))
            if m.group(2) and method == "POST":
                return self.api_source_refresh(sid)
            if not m.group(2) and method == "PUT":
                return self.api_source_update(sid)
            if not m.group(2) and method == "DELETE":
                return self.api_source_delete(sid)
        raise HTTPError(404, "Not found.")

    def api_login(self):
        data = self.read_json()
        if not self.app.check_login(str(data.get("username", "")), str(data.get("password", ""))):
            raise HTTPError(401, "Wrong username or password.")
        cookie = f"{SESSION_COOKIE}={self.app.make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
        if self.is_https():
            cookie += "; Secure"
        self.send_json({"ok": True}, extra={"Set-Cookie": cookie})

    def api_logout(self):
        self.send_json(
            {"ok": True},
            extra={
                "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            },
        )

    # ---------------- overview

    def api_overview(self):
        engine = self.app.engine
        catalog = engine.catalog()
        selected = engine.selected_keys()
        sources = [
            dict(r)
            for r in self.app.db.query(
                "SELECT id, kind, name, enabled, last_success, last_error, last_attempt, item_count FROM sources ORDER BY kind, priority, id"
            )
        ]
        playlist = self.app.db.get_kv("playlist_stats", {}) or {}
        epg = self.app.db.get_kv("epg_stats", {}) or {}
        self.send_json(
            {
                "available": len(catalog),
                "selected": len(selected),
                "selected_available": sum(1 for k in selected if k in catalog),
                "sources": sources,
                "playlist": playlist,
                "epg": epg,
                "feed_warnings": feed_warnings(playlist, epg),
                "feeds": self.app.feed_urls(self.base_url()),
                "job": engine.current_job,
                "pending": engine.pending(),
                "metadata": self.app.db.get_kv("metadata_state", {}) or {},
                "next_refresh": engine._next_scheduled(),
                "version": __version__,
            }
        )

    # ---------------- channels

    def _filtered(self, f: dict) -> tuple:
        engine = self.app.engine
        catalog = engine.catalog()
        selected = engine.selection_numbers()  # key -> channel number
        coverage = self.app.db.get_kv("epg_coverage", {}) or {}
        terms = m3u.fold(str(f.get("q") or "")).split()
        source = str(f.get("source") or "")
        category = str(f.get("category") or "")
        country = str(f.get("country") or "").upper()
        language = str(f.get("language") or "").lower()
        status = str(f.get("status") or "all")
        epg = str(f.get("epg") or "")
        items = list(catalog.values())
        if status == "selected":
            # Selected channels currently missing upstream have a name and nothing else, so
            # only the search terms can match them; any other filter leaves them out.
            other_filters = source or category or country or language or epg
            missing = []
            if not other_filters:
                own = {
                    r["channel_key"]: r["name"]
                    for r in self.app.db.query("SELECT channel_key, name FROM channel_edits")
                    if r["name"]
                }
                for r in self.app.db.query("SELECT channel_key, name, number FROM selections"):
                    key = r["channel_key"]
                    names = m3u.fold(f"{r['name']} {own.get(key, '')}")
                    if key not in catalog and all(t in names for t in terms):
                        name = own.get(key) or r["name"]
                        missing.append({"key": key, "name": name, "number": r["number"]})
            items = [c for c in items if c.key in selected]
        else:
            missing = []
        out = []
        for ch in items:
            if terms and not all(t in ch.search for t in terms):
                continue
            if source and (not source.isdigit() or int(source) not in ch.sources):
                continue
            if category and category not in ch.shown_categories:
                continue
            if country and country not in ch.countries:
                continue
            if language and language not in ch.languages:
                continue
            if status == "unselected" and ch.key in selected:
                continue
            if epg == "yes" and ch.key not in coverage:
                continue
            if epg == "no" and ch.key in coverage:
                continue
            out.append(ch)
        out.sort(key=lambda c: (m3u.fold(c.feed_name), c.key))
        missing.sort(key=lambda r: (m3u.fold(r["name"]), r["key"]))
        if f.get("sort") == "number":  # selected channels first, by number; then by name
            out.sort(key=lambda c: (c.key not in selected, selected.get(c.key, 0)))
            missing.sort(key=lambda r: r["number"])
        return out, missing, selected, coverage

    def api_channels(self, q: dict):
        items, missing, selected, coverage = self._filtered(q)
        try:
            offset = max(0, int(q.get("offset", 0)))
            limit = min(500, max(1, int(q.get("limit", 100))))
        except ValueError:
            raise HTTPError(400, "Bad paging parameters.") from None
        catalog = self.app.engine.catalog()
        facets = {"categories": {}, "countries": {}, "languages": {}}
        for ch in catalog.values():
            for name, values in (
                ("categories", ch.shown_categories),
                ("countries", ch.countries),
                ("languages", ch.languages),
            ):
                for v in values:
                    facets[name][v] = facets[name].get(v, 0) + 1
        page = items[offset : offset + limit]
        self.send_json(
            {
                "total": len(items),
                "offset": offset,
                "available": len(catalog),
                "selected": len(selected),
                "missing": missing if offset == 0 else [],
                "items": [
                    {
                        "key": c.key,
                        "number": selected.get(c.key),  # None while not selected
                        "name": c.feed_name,  # what the feeds call it
                        "source_name": c.name,
                        "name_edited": bool(c.own_name),
                        "tvg_id": c.tvg_id,
                        "logo": c.feed_logo,
                        "source_logo": c.logo,
                        "logo_edited": bool(c.own_logo),
                        "categories": c.shown_categories,
                        "source_categories": c.categories,
                        "feed_categories": c.feed_categories,
                        "category_edited": bool(c.own_categories),
                        "countries": c.countries,
                        "languages": c.languages,
                        "sources": c.sources,
                        "streams": c.streams,
                        "selected": c.key in selected,
                        "epg": coverage.get(c.key),
                    }
                    for c in page
                ],
                "facets": {
                    k: sorted(v.items(), key=lambda kv: (-kv[1], kv[0])) for k, v in facets.items()
                },
            }
        )

    def api_channel(self, q: dict):
        key = q.get("key") or ""
        rows = self.app.db.query(
            """SELECT s.source_id, src.name AS source_name, src.enabled, s.title, s.url, s.attrs, s.directives
               FROM streams s JOIN sources src ON src.id = s.source_id
               WHERE s.channel_key = ? ORDER BY src.priority, src.id, s.pos""",
            (key,),
        )
        seen = set()
        streams = []
        for r in rows:
            directives = json.loads(r["directives"])
            sig = m3u.stream_signature(r["url"], directives)
            enabled = bool(r["enabled"])
            streams.append(
                {
                    "source": r["source_name"],
                    "enabled": enabled,
                    "title": r["title"],
                    "url": r["url"],
                    "group": json.loads(r["attrs"]).get("group-title", ""),
                    "directives": directives,
                    # Disabled sources feed no output: they neither mark nor get marked.
                    "duplicate": enabled and sig in seen,
                }
            )
            if enabled:
                seen.add(sig)
        engine = self.app.engine
        number = engine.selection_numbers().get(key)
        # The feed's streams for this channel in feed order, switched-off ones included;
        # `sig` is what PUT /api/channel/streams takes back. AIOStreams keeps one stream per
        # URL (the first), so `url_repeat` marks one whose URL an earlier on stream has.
        prefs = engine.stream_prefs([key]).get(key, {})
        feed_streams = []
        urls: set[str] = set()
        for sig, row, on in engine.channel_streams(key, prefs):
            url = row["url"].strip()
            feed_streams.append(
                {
                    "sig": sig,
                    "source": row["source"],
                    "url": row["url"],
                    "directives": json.loads(row["directives"]),
                    "on": on,
                    "url_repeat": url in urls,
                }
            )
            if on:
                urls.add(url)
        self.send_json(
            {
                "key": key,
                "number": number,
                **self._shown(key),
                "streams": streams,
                "feed_streams": feed_streams,
                "streams_edited": bool(prefs),
            }
        )

    def api_selection(self):
        data = self.read_json()
        engine = self.app.engine
        if "filter" in data:
            if not isinstance(data["filter"], dict) or data.get("action") not in (
                "select",
                "deselect",
            ):
                raise HTTPError(400, "Bulk selection needs filter and action.")
            items, missing, _, _ = self._filtered(data["filter"])
            keys = [c.key for c in items]
            if data["action"] == "deselect":
                keys += [r["key"] for r in missing]
            if len(keys) > BULK_LIMIT:
                raise HTTPError(
                    400,
                    f"Refusing to change more than {BULK_LIMIT} channels at once; narrow the filter.",
                )
            select, deselect = (keys, []) if data["action"] == "select" else ([], keys)
        else:
            select = [str(k) for k in data.get("select") or []][:BULK_LIMIT]
            deselect = [str(k) for k in data.get("deselect") or []][:BULK_LIMIT]
        changed = engine.set_selection(select, deselect)
        self.send_json({"changed": changed, "selected": len(engine.selected_keys())})

    def api_channel_number(self):
        data = self.read_json()
        key = str(data.get("key") or "")
        number = data.get("number")
        if type(number) is not int or not 1 <= number <= MAX_NUMBER:
            raise HTTPError(400, f"Number must be a whole number from 1 to {MAX_NUMBER}.")
        try:
            self.app.engine.set_number(key, number)
        except KeyError:
            raise HTTPError(404, "Channel is not selected.") from None
        except ValueError as exc:
            raise HTTPError(409, str(exc)) from None
        self.send_json({"ok": True, "number": number})

    def api_channel_streams(self):
        data = self.read_json()
        key = str(data.get("key") or "")
        if "streams" not in data:  # an absent field must not reset saved choices
            raise HTTPError(400, "Streams are required (a list, or null for source order).")
        raw = data["streams"]
        choices = None
        if raw is not None:
            if not isinstance(raw, list):
                raise HTTPError(400, "Streams must be a list.")
            if len(raw) > BULK_LIMIT:
                raise HTTPError(400, f"At most {BULK_LIMIT} streams.")
            choices = []
            for item in raw:
                if not (
                    isinstance(item, dict)
                    and isinstance(item.get("sig"), str)
                    and isinstance(item.get("on"), bool)
                ):
                    raise HTTPError(400, "Each stream needs sig (text) and on (true or false).")
                choices.append((item["sig"], item["on"]))
        try:
            self.app.engine.set_streams(key, choices)
        except KeyError:
            raise HTTPError(404, "Channel is not selected.") from None
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        self.send_json({"ok": True, "streams_edited": bool(choices)})

    def api_channel_edit(self):
        """Own name, logo and/or categories in one write; a field left out is kept, "" or
        null resets it."""
        data = self.read_json()
        key = str(data.get("key") or "")
        fields = {}
        if "name" in data:
            fields["name"] = self._rule(rules.edit_name, data["name"])
        if "logo" in data:
            logo = data["logo"]
            own = self.app.db.one("SELECT logo FROM channel_edits WHERE channel_key = ?", (key,))
            # a saved http logo keeps working: only a changed value must be https
            if own and own["logo"] and logo == own["logo"]:
                fields["logo"] = logo
            else:
                fields["logo"] = self._rule(rules.edit_logo, logo)
        if "categories" in data:
            cats = data["categories"]
            fields["categories"] = None if cats is None else self._rule(rules.categories, cats)
        if not fields:
            raise HTTPError(400, "Send name, logo, categories or any of them.")
        try:
            stored = self.app.engine.set_edit(key, **fields)
        except KeyError:
            raise HTTPError(404, "Unknown channel.") from None
        except ValueError as e:
            raise HTTPError(400, str(e)) from None
        shown = self._shown(key)
        out = {k: shown[k] for k in ("name", "name_edited", "logo", "logo_edited")}
        if "categories" in fields:
            ch = self.app.engine.catalog().get(key)
            cats = stored["categories"]
            if ch is None:  # a refresh dropped the channel after the save
                out["categories"] = cats or []
                out["feed_categories"] = out["categories"][:MAX_CATEGORIES]
                out["source_categories"] = []
            else:
                out["categories"], out["feed_categories"] = ch.shown_categories, ch.feed_categories
                out["source_categories"] = ch.categories
            out["category_edited"] = cats is not None
        self.send_json({"ok": True, **out})

    def _shown(self, key: str) -> dict:
        """A channel's name and logo as the feeds give them, with the source values. A selected
        channel missing upstream shows its own name, else the name stored when selected."""
        ch = self.app.engine.catalog().get(key)
        edit = self.app.db.one("SELECT name, logo FROM channel_edits WHERE channel_key = ?", (key,))
        own_name, own_logo = (edit["name"], edit["logo"]) if edit else (None, None)
        if ch is not None:
            source_name, source_logo = ch.name, ch.logo
        else:
            sel = self.app.db.one("SELECT name FROM selections WHERE channel_key = ?", (key,))
            source_name, source_logo = (sel["name"] if sel else None), None
        return {
            "name": own_name or source_name,
            "source_name": source_name,
            "name_edited": own_name is not None,
            "logo": own_logo or source_logo,
            "source_logo": source_logo,
            "logo_edited": own_logo is not None,
        }

    # ---------------- sources

    def api_sources(self):
        rows = self.app.db.query("SELECT * FROM sources ORDER BY kind, priority, id")
        pending = set(self.app.engine.pending()["refresh"])
        # Selected channels each enabled source feeds: a playlist that has a stream for
        # the channel, the one guide the last EPG build took its programmes from.
        used = {
            r["source_id"]: r["n"]
            for r in self.app.db.query(
                """SELECT s.source_id, COUNT(DISTINCT s.channel_key) AS n
                   FROM streams s JOIN selections sel ON sel.channel_key = s.channel_key
                   GROUP BY s.source_id"""
            )
        }
        selected = self.app.engine.selected_keys()
        for key, cov in (self.app.db.get_kv("epg_coverage", {}) or {}).items():
            if key in selected:
                used[cov["source_id"]] = used.get(cov["source_id"], 0) + 1
        out = []
        for r in rows:
            d = dict(r)
            d.pop("etag", None)
            d.pop("last_modified", None)
            d.pop("fragment_key", None)
            d["pending"] = r["id"] in pending
            d["channels_used"] = used.get(r["id"], 0) if r["enabled"] else None
            out.append(d)
        self.send_json({"sources": out, "job": self.app.engine.current_job})

    @staticmethod
    def _rule(rule, *args):
        """Run one of the shared input rules; a refusal is a 400 with its message."""
        try:
            return rule(*args)
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None

    def api_source_create(self):
        fields = self._rule(rules.source_fields, self.read_json(), False)
        fields.setdefault("priority", 100)
        with self.app.db.conn() as c:
            cur = c.execute(
                "INSERT INTO sources (kind, name, url, enabled, priority, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    fields["kind"],
                    fields["name"],
                    fields["url"],
                    fields.get("enabled", 1),
                    fields["priority"],
                    time.time(),
                ),
            )
        self.app.engine.request_refresh([cur.lastrowid])
        self.send_json({"id": cur.lastrowid}, 201)

    def api_source_update(self, sid: int):
        src = self.app.db.one("SELECT * FROM sources WHERE id = ?", (sid,))
        if not src:
            raise HTTPError(404, "Source not found.")
        fields = self._rule(rules.source_fields, self.read_json(), True)
        if fields.pop("kind", src["kind"]) != src["kind"]:
            raise HTTPError(400, "A source's kind cannot be changed; add a new source instead.")
        if not fields:
            return self.send_json({"ok": True})
        url_changed = "url" in fields and fields["url"] != src["url"]
        if url_changed:
            fields.update(etag=None, last_modified=None)
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.app.db.conn() as c:
            c.execute(f"UPDATE sources SET {cols} WHERE id = ?", (*fields.values(), sid))
        engine = self.app.engine
        engine.invalidate_catalog()
        if url_changed or fields.get("enabled") == 1:
            engine.request_refresh([sid])
        engine.request_playlist()
        engine.request_epg()
        self.send_json({"ok": True})

    def api_source_delete(self, sid: int):
        with self.app.db.conn() as c:
            deleted = c.execute("DELETE FROM sources WHERE id = ?", (sid,)).rowcount
        if not deleted:
            raise HTTPError(404, "Source not found.")
        engine = self.app.engine
        engine.raw_path(sid).unlink(missing_ok=True)
        engine.fragment_path(sid).unlink(missing_ok=True)
        engine.invalidate_catalog()
        engine.request_playlist()
        engine.request_epg()
        self.send_json({"ok": True})

    def api_source_refresh(self, sid: int):
        if not self.app.db.one("SELECT 1 FROM sources WHERE id = ?", (sid,)):
            raise HTTPError(404, "Source not found.")
        self.app.engine.request_refresh([sid])
        self.send_json({"ok": True}, 202)

    def api_refresh_all(self):
        self.app.engine.request_refresh(None)
        self.send_json({"ok": True}, 202)

    # ---------------- EPG

    def api_epg(self):
        engine = self.app.engine
        catalog = engine.catalog()
        coverage = self.app.db.get_kv("epg_coverage", {}) or {}
        mapping_rows = self.app.db.query("SELECT * FROM epg_mappings")
        mappings = {r["channel_key"]: r["epg_id"] for r in mapping_rows}
        mapping_sources = {r["channel_key"]: r["source_id"] for r in mapping_rows}
        sources = {
            r["id"]: r["name"]
            for r in self.app.db.query("SELECT id, name FROM sources WHERE kind = 'xmltv'")
        }
        rows = []
        selections = self.app.db.query(
            "SELECT channel_key, name, number FROM selections ORDER BY added_at, channel_key"
        )
        own = {
            r["channel_key"]: r["name"]
            for r in self.app.db.query("SELECT channel_key, name FROM channel_edits")
            if r["name"]
        }
        missing = [
            catalog[r["channel_key"]]
            for r in selections
            if r["channel_key"] in catalog
            and r["channel_key"] not in coverage
            and mappings.get(r["channel_key"]) != ""
        ]
        diagnosis = engine.diagnose(missing, mappings)
        export_ids = engine.exported_ids(catalog, [r["channel_key"] for r in selections])
        for r in selections:
            ch = catalog.get(r["channel_key"])
            cov = coverage.get(r["channel_key"])
            rows.append(
                {
                    "key": r["channel_key"],
                    "number": r["number"],
                    "name": ch.feed_name if ch else own.get(r["channel_key"]) or r["name"],
                    "tvg_id": ch.tvg_id if ch else "",
                    "out_id": export_ids.get(r["channel_key"], ch.out_id) if ch else "",
                    "available": ch is not None,
                    "candidates": ch.candidates() if ch else [],
                    "mapping": mappings.get(r["channel_key"]),
                    "mapping_source": sources.get(mapping_sources.get(r["channel_key"])),
                    "coverage": {**cov, "source": sources.get(cov["source_id"], "?")}
                    if cov
                    else None,
                    "diagnosis": diagnosis.get(r["channel_key"]),
                }
            )
        rows.sort(key=lambda x: (x["coverage"] is not None, m3u.fold(x["name"])))
        engine.now_playing(
            [r["coverage"] for r in rows if r["coverage"]]
            + [s for r in rows if r["diagnosis"] for s in r["diagnosis"]["suggestions"]]
        )
        self.send_json(
            {
                "channels": rows,
                "stats": self.app.db.get_kv("epg_stats", {}) or {},
                "has_sources": bool(sources),
                "guide_sources": engine.guide_source_status(),
                "feed_warnings": feed_warnings(
                    self.app.db.get_kv("playlist_stats", {}) or {},
                    self.app.db.get_kv("epg_stats", {}) or {},
                ),
                "job": engine.current_job,
                "epg_pending": engine.epg_pending(),
            }
        )

    def api_epg_search(self, q: dict):
        term = xmltv.norm_id(m3u.fold(str(q.get("q") or "")))
        if len(re.sub(r"[\W_]+", "", term)) < 2:
            return self.send_json({"results": []})

        def like(value: str) -> str:
            return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        compact = like(re.sub(r"[\s\-_.]+", "", term))
        rows = self.app.db.query(
            """SELECT e.epg_id, e.display_name, e.programmes, s.name AS source, e.source_id
               FROM epg_channels e JOIN sources s ON s.id = e.source_id
               WHERE s.enabled = 1 AND s.kind = 'xmltv' AND (
                     e.epg_id_lc LIKE ?1 ESCAPE '\\' OR fold(e.display_name) LIKE ?1 ESCAPE '\\'
                  OR replace(e.epg_id_lc, '.', '') LIKE ?2 ESCAPE '\\'
                  OR replace(replace(fold(e.display_name), ' ', ''), '-', '') LIKE ?2 ESCAPE '\\')
               ORDER BY e.programmes = 0, s.priority, e.display_name LIMIT 50""",
            (like(term), compact),
        )
        results = [dict(r) for r in rows]
        self.app.engine.now_playing(results)
        self.send_json({"results": results})

    def api_epg_mapping(self):
        data = self.read_json()
        key = str(data.get("key") or "")
        if not self.app.db.one("SELECT 1 FROM selections WHERE channel_key = ?", (key,)):
            raise HTTPError(404, "Channel is not selected.")
        epg_id = data.get("epg_id")
        source_id = data.get("source_id")
        if source_id is not None:
            if type(source_id) is not int or not self.app.db.one(
                "SELECT 1 FROM sources WHERE id = ? AND kind = 'xmltv'", (source_id,)
            ):
                raise HTTPError(400, "Unknown guide source.")
        with self.app.db.conn() as c:
            if epg_id is None:
                c.execute("DELETE FROM epg_mappings WHERE channel_key = ?", (key,))
            else:
                epg_id = self._rule(rules.epg_id, epg_id)
                c.execute(
                    "INSERT INTO epg_mappings (channel_key, epg_id, source_id) VALUES (?, ?, ?)"
                    " ON CONFLICT(channel_key) DO UPDATE SET epg_id = excluded.epg_id,"
                    " source_id = excluded.source_id",
                    (key, epg_id, source_id if epg_id else None),
                )
        self.app.engine.request_epg(delay=5)
        self.send_json({"ok": True})

    def api_epg_rebuild(self):
        self.app.engine.request_epg(force_scan=True)
        self.send_json({"ok": True}, 202)

    # ---------------- settings

    def api_settings(self):
        cfg = self.app.cfg
        self.send_json(
            {
                "settings": self.app.db.settings(),
                "env": {
                    "refresh_seconds": cfg.refresh_seconds,
                    "request_timeout": cfg.request_timeout,
                    "m3u_max_mb": cfg.m3u_max_bytes // 2**20,
                    "epg_max_download_mb": cfg.epg_max_download_bytes // 2**20,
                    "epg_max_decompressed_mb": cfg.epg_max_decompressed_bytes // 2**20,
                    "allow_private_sources": cfg.allow_private_sources,
                    "public_url": cfg.public_url,
                    "admin_user": cfg.admin_user,
                },
                "feeds": self.app.feed_urls(self.base_url()),
            }
        )

    def api_settings_update(self):
        data = self.read_json()
        current = self.app.db.settings()
        new = dict(current)
        for key, value in data.items():
            new[key] = self._rule(rules.setting, key, value)
        self.app.db.set_kv("settings", new)
        engine = self.app.engine
        if new["playlist_epg_header"] != current["playlist_epg_header"]:
            engine.request_playlist()
        if (new["epg_days"], new["epg_past_hours"]) != (
            current["epg_days"],
            current["epg_past_hours"],
        ):
            engine.request_epg()
        if (
            new["enrich_languages"] != current["enrich_languages"]
            or new["metadata_url"] != current["metadata_url"]
        ):
            if not new["enrich_languages"]:
                with self.app.db.conn() as c:
                    c.execute("DELETE FROM meta_languages")
                engine.clear_metadata_error()
                engine.invalidate_catalog()
                engine.request_playlist()
            else:
                engine.request_metadata()
        self.send_json({"settings": new})

    # ---------------- backup

    def api_backup(self):
        # Rows go to an anonymous temp file (never a named one holding secrets), so the
        # response has a Content-Length and memory stays flat.
        with tempfile.TemporaryFile() as fh:
            backup.export(self.app.db, fh)
            size = fh.tell()
            if size > backup.MAX_BYTES:  # a file no import accepts is no backup
                raise HTTPError(
                    413,
                    f"The backup is {size / 2**20:.1f} MiB, over the "
                    f"{backup.MAX_BYTES // 2**20} MiB an import accepts, so it was not downloaded.",
                )
            fh.seek(0)
            name = time.strftime("aiolineup-backup-%Y-%m-%d.json", time.gmtime())
            self._base_headers(
                200,
                "application/json",
                size,
                {
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="{name}"',
                },
            )
            if self.command != "HEAD":
                shutil.copyfileobj(fh, self.wfile)

    def api_backup_import(self, query: dict):
        """The file as the request body. ?check=1 validates it and returns the summary only."""
        doc = self.read_json(backup.MAX_BYTES)
        try:
            parsed = backup.check(doc)
        except backup.BackupError as exc:
            raise HTTPError(400, str(exc)) from None
        del doc
        summary = parsed.summary()
        if query.get("check") == "1":
            return self.send_json(summary)
        try:
            refreshing = backup.apply(self.app.engine, parsed)
        except Busy:
            raise HTTPError(409, "A refresh is running; try again when it finishes.") from None
        log.info("backup imported: %s", summary["counts"])  # counts only, never contents
        self.send_json({"ok": True, **summary, "refreshing": refreshing})


def serve(cfg: Config, engine: Engine) -> Server:
    return Server((cfg.host, cfg.port), App(cfg, engine))
