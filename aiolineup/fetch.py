"""Hardened HTTP downloads for upstream sources.

* Only http/https, no embedded credentials.
* Every hop (including redirects) is resolved and checked; private, loopback,
  link-local and other non-global addresses are refused unless explicitly allowed.
  The connection is pinned to the vetted IP, so DNS rebinding between the check
  and the connect cannot redirect us to an internal host.
* Hard limits on response size, per-operation socket timeout and total duration. The
  total deadline covers every hop, connect attempt, TLS handshake, header and body read:
  a watchdog shuts the socket down when it passes, so a slow or trickling server cannot
  hold the single worker beyond it.
* Conditional requests (ETag / Last-Modified) so unchanged sources cost one round trip.
"""

from __future__ import annotations

import gzip
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

from . import __version__

USER_AGENT = f"aiolineup/{__version__}"
MAX_REDIRECTS = 5
TOTAL_DEADLINE = 15 * 60  # seconds, whole download
CHUNK = 64 * 1024
TOO_LONG = "Download took too long and was aborted."


class FetchError(Exception):
    """A user-presentable download failure."""


class NotModified(Exception):
    """Upstream answered 304; the cached copy is still current."""


class SizeLimitExceeded(FetchError):
    pass


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    etag: str | None
    last_modified: str | None
    size: int


def validate_url(url: str) -> str:
    """Return a normalised URL or raise ValueError with a human message."""
    url = (url or "").strip()
    if len(url) > 2048:
        raise ValueError("URL is too long.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// URLs are supported.")
    if not parts.hostname:
        raise ValueError("URL has no host.")
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not supported.")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ValueError("URL contains whitespace or control characters.")
    try:
        parts.port  # noqa: B018 - raises on invalid port
    except ValueError as exc:
        raise ValueError("URL has an invalid port.") from exc
    return url


# IPv6 ranges whose last 32 bits are an IPv4 address that a gateway or the stack may
# reach: NAT64 well-known prefix, IPv4-translated (SIIT) and deprecated IPv4-compatible.
# Python counts the first two as global, so the embedded address is checked instead.
_EMBEDDED_IPV4 = tuple(
    ipaddress.ip_network(n) for n in ("64:ff9b::/96", "::ffff:0:0:0/96", "::/96")
)


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _EMBEDDED_IPV4):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int, allow_private: bool) -> list[str]:
    """Every address of `host`, in resolver order. Refuses the host if any address is
    non-public, so the result is safe to connect to in any order."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve host {host}.") from exc
    addresses = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not allow_private and not _is_public(ip):
            raise FetchError(
                f"Host {host} resolves to a non-public address; refusing to fetch "
                "(set ALLOW_PRIVATE_SOURCES=true to allow LAN sources)."
            )
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise FetchError(f"Could not resolve host {host}.")
    return addresses


class _Deadline:
    """The whole download's time limit. Connect attempts get at most the time left, and a
    timer shuts every connected socket down once it passes: a socket timeout only bounds
    one operation, so a server trickling its TLS handshake, headers or body a byte at a
    time could otherwise outlast it. `expired` tells the resulting EOF or error apart."""

    def __init__(self, seconds: float):
        self.at = time.monotonic() + seconds
        self.expired = False
        self._lock = threading.Lock()
        self._socks: list[socket.socket] = []
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def timeout(self, timeout: float) -> float:
        """`timeout`, capped by the time left; raises once none is left."""
        left = self.at - time.monotonic()
        if left <= 0 or self.expired:
            raise FetchError(TOO_LONG)
        return min(timeout, left)

    def watch(self, sock: socket.socket) -> None:
        # A duplicate descriptor still reaches the connection after wrap_socket detaches
        # the original, and shutdown() acts on the connection, not the descriptor.
        with self._lock:
            dup = sock.dup()
            self._socks.append(dup)
            if self.expired:
                _shutdown(dup)

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            for s in self._socks:
                _shutdown(s)

    def release(self) -> None:
        """Close the duplicates once a hop's connection is done with: a duplicate keeps the
        TCP connection open, and an origin may wait for it to close before the next hop."""
        with self._lock:
            for s in self._socks:
                s.close()
            self._socks.clear()

    def close(self) -> None:
        self._timer.cancel()
        self.release()


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed by the peer


def _connect_pinned(
    ips: list[str], port: int, timeout: float, deadline: _Deadline
) -> socket.socket:
    """Connect to the first reachable vetted address (e.g. IPv4 when IPv6 has no route)."""
    error: OSError | None = None
    for ip in ips:
        try:
            sock = socket.create_connection((ip, port), deadline.timeout(timeout))
        except OSError as exc:
            error = exc
            continue
        try:
            deadline.watch(sock)
        except OSError:
            sock.close()
            raise
        return sock
    raise error


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ips: list[str], timeout: float, deadline: _Deadline):
        super().__init__(host, port, timeout=timeout)
        self._ips = ips
        self._deadline = deadline

    def connect(self) -> None:
        self.sock = _connect_pinned(self._ips, self.port, self.timeout, self._deadline)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ips: list[str], timeout: float, deadline: _Deadline):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ips = ips
        self._deadline = deadline

    def connect(self) -> None:
        sock = _connect_pinned(self._ips, self.port, self.timeout, self._deadline)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _open(
    url: str, headers: dict[str, str], timeout: float, allow_private: bool, deadline: _Deadline
):
    parts = urlsplit(url)
    https = parts.scheme == "https"
    port = parts.port or (443 if https else 80)
    ips = resolve(parts.hostname, port, allow_private)
    cls = _PinnedHTTPS if https else _PinnedHTTP
    conn = cls(parts.hostname, port, ips, timeout, deadline)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request("GET", path, headers=headers)
        return conn, conn.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        if deadline.expired:
            raise FetchError(TOO_LONG) from exc
        raise FetchError(f"Could not connect to {parts.hostname}: {_describe(exc)}") from exc
    except BaseException:
        conn.close()
        raise


def _describe(exc: BaseException) -> str:
    if isinstance(exc, socket.timeout | TimeoutError):
        return "timed out"
    if isinstance(exc, ssl.SSLError):
        return f"TLS error ({exc.reason or exc})"
    return str(exc) or exc.__class__.__name__


def download(
    url: str,
    sink: Callable[[bytes], None],
    *,
    timeout: float,
    max_bytes: int,
    allow_private: bool = False,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """Stream `url` into `sink`. Raises NotModified, FetchError."""
    url = validate_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    deadline = _Deadline(TOTAL_DEADLINE)
    try:
        return _follow(url, headers, sink, timeout, max_bytes, allow_private, deadline)
    finally:
        deadline.close()


def _follow(
    url: str,
    headers: dict[str, str],
    sink: Callable[[bytes], None],
    timeout: float,
    max_bytes: int,
    allow_private: bool,
    deadline: _Deadline,
) -> FetchResult:
    for _ in range(MAX_REDIRECTS + 1):
        deadline.timeout(timeout)  # no time left for another hop
        conn, resp = _open(url, headers, timeout, allow_private, deadline)
        try:
            # The header parser takes the watchdog's EOF as the end of the headers: never
            # act on a status (a 304 above all) whose headers the deadline cut off.
            deadline.timeout(timeout)
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise FetchError(f"Redirect (HTTP {resp.status}) without a Location header.")
                try:
                    url = validate_url(urljoin(url, location))
                except ValueError as exc:
                    raise FetchError(f"Refusing redirect: {exc}") from exc
                continue
            if resp.status == 304:
                raise NotModified()
            if resp.status != 200:
                raise FetchError(f"Upstream returned HTTP {resp.status} {resp.reason}.")

            length = resp.getheader("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise SizeLimitExceeded(
                    f"Upstream file is {int(length) // (1024 * 1024)} MiB, over the "
                    f"{max_bytes // (1024 * 1024)} MiB download limit."
                )
            total = 0
            while True:
                deadline.timeout(timeout)
                try:
                    chunk = resp.read(CHUNK)
                except (OSError, http.client.HTTPException) as exc:
                    if deadline.expired:
                        raise FetchError(TOO_LONG) from exc
                    raise FetchError(f"Download interrupted: {_describe(exc)}") from exc
                if deadline.expired:  # the watchdog's shutdown reads as a clean EOF
                    raise FetchError(TOO_LONG)
                if not chunk:
                    if length and length.isdigit() and total != int(length):
                        raise FetchError("Download ended early (connection closed mid-body).")
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SizeLimitExceeded(
                        f"Download exceeded the {max_bytes // (1024 * 1024)} MiB limit."
                    )
                sink(chunk)
            return FetchResult(
                url=url,
                etag=resp.getheader("ETag"),
                last_modified=resp.getheader("Last-Modified"),
                size=total,
            )
        finally:
            resp.close()  # owns the socket when the server said Connection: close
            conn.close()
            deadline.release()
    raise FetchError("Too many redirects.")


def download_to_file(url: str, dest: Path, **kwargs) -> FetchResult:
    with open(dest, "wb") as fh:
        return download(url, fh.write, **kwargs)


def download_bytes(url: str, **kwargs) -> tuple[bytes, FetchResult]:
    """Download into memory and transparently gunzip (bounded by max_bytes)."""
    buf = bytearray()
    result = download(url, buf.extend, **kwargs)
    return gunzip_bounded(buf, kwargs["max_bytes"]), result


def gunzip_bounded(data: bytes | bytearray, max_bytes: int) -> bytes | bytearray:
    if data[:2] != b"\x1f\x8b":
        return data  # no copy
    out = bytearray()
    while data:  # loop handles multi-member gzip files
        d = zlib.decompressobj(wbits=31)
        try:
            out += d.decompress(data, max_bytes + 1 - len(out))
        except zlib.error as exc:
            raise FetchError(f"Corrupt gzip data: {exc}") from exc
        if len(out) > max_bytes or d.unconsumed_tail:
            raise SizeLimitExceeded(f"Decompressed size exceeds {max_bytes // (1024 * 1024)} MiB.")
        if not d.eof:  # input ran out mid-member: never pass a cut-off playlist on
            raise FetchError("Truncated gzip data.")
        data = d.unused_data
    return bytes(out)


class BoundedReader:
    """File-like wrapper that raises once more than `limit` bytes were read."""

    def __init__(self, raw: BinaryIO, limit: int):
        self._raw = raw
        self._limit = limit
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size if size and size > 0 else CHUNK)
        self.count += len(data)
        if self.count > self._limit:
            raise SizeLimitExceeded(
                f"Decompressed guide exceeds the {self._limit // (1024 * 1024)} MiB limit."
            )
        return data


def open_maybe_gzip(path: Path, limit: int) -> tuple[BinaryIO, BoundedReader]:
    """Open a cached download, transparently decompressing gzip, with a size cap."""
    fh = open(path, "rb")
    magic = fh.read(2)
    fh.seek(0)
    raw: BinaryIO = gzip.GzipFile(fileobj=fh, mode="rb") if magic == b"\x1f\x8b" else fh
    return fh, BoundedReader(raw, limit)
