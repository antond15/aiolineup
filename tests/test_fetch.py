import gzip
import ipaddress
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from aiolineup import fetch


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif self.path == "/redirect-file":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
        elif self.path == "/redirect-ok":
            self.send_response(301)
            self.send_header("Location", "/data")
            self.end_headers()
        elif self.path == "/data":
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_response(304)
                self.end_headers()
                return
            body = b"#EXTM3U\n"
            self.send_response(200)
            self.send_header("ETag", '"v1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/big":
            self.send_response(200)
            self.end_headers()  # no Content-Length: must be stopped while streaming
            self.wfile.write(b"x" * 300_000)
        elif self.path == "/short":
            self.send_response(200)
            self.send_header("Content-Length", "150")
            self.end_headers()
            self.wfile.write(b"#EXTM3U\n#EXTINF:-1,A\nhttp://a\n")
            self.close_connection = True
        elif self.path in ("/trickle-body", "/trickle-headers", "/trickle-304"):
            # One byte at a time, each well inside the socket timeout: only the total
            # deadline can stop it. Gives up after ~20 s if the client never does.
            self.send_response(304 if self.path == "/trickle-304" else 200)
            if self.path != "/trickle-body":
                self.flush_headers()
                data = b"X-Slow: " + b"a" * 400
            else:
                self.end_headers()
                data = b"x" * 400
            try:
                for byte in data:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            self.close_connection = True
        elif self.path == "/gz":
            body = gzip.compress(b"#EXTM3U\nhello\n")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(500)
            self.end_headers()


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def get(self, path, **kw):
        kw.setdefault("timeout", 5)
        kw.setdefault("max_bytes", 100_000)
        kw.setdefault("allow_private", True)
        return fetch.download_bytes(self.base + path, **kw)

    def test_validate_url(self):
        for bad in (
            "ftp://x/y",
            "file:///etc/passwd",
            "http://",
            "https://u:p@h/x",
            "http://h/x y",
            "javascript:alert(1)",
        ):
            with self.assertRaises(ValueError, msg=bad):
                fetch.validate_url(bad)
        self.assertEqual(
            fetch.validate_url(" https://iptv-org.github.io/iptv/x.m3u "),
            "https://iptv-org.github.io/iptv/x.m3u",
        )

    def test_private_addresses_refused_by_default(self):
        for host in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "[::1]", "localhost", "0.0.0.0"):
            with self.assertRaises(fetch.FetchError, msg=host):
                fetch.download_bytes(f"http://{host}/", timeout=2, max_bytes=10)

    def test_embedded_ipv4_checked(self):
        # NAT64 (64:ff9b::/96), IPv4-translated (::ffff:0:0:0/96) and IPv4-compatible
        # (::/96) addresses carry an IPv4 address in their last 32 bits: that one is checked.
        for addr, public in (
            ("64:ff9b::808:808", True),
            ("64:ff9b::7f00:1", False),
            ("64:ff9b::a00:1", False),
            ("64:ff9b::a9fe:a9fe", False),
            ("::ffff:0:808:808", True),
            ("::ffff:0:7f00:1", False),
            ("::ffff:0:c0a8:1", False),
            ("::8.8.8.8", True),
            ("::127.0.0.1", False),
            ("::ffff:127.0.0.1", False),
            ("::ffff:8.8.8.8", True),
            ("::", False),
            ("::1", False),
            ("64:ff9b:1::a00:1", False),
            ("2606:4700::1111", True),
        ):
            with self.subTest(addr):
                self.assertEqual(fetch._is_public(ipaddress.ip_address(addr)), public)

    def test_every_vetted_address_is_tried(self):
        # A dual-stack host whose first address is unreachable falls through to the next one;
        # every address is still vetted before any connection is made.
        port = self.server.server_address[1]
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),  # TEST-NET, dead
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]
        with mock.patch("socket.getaddrinfo", return_value=infos):
            self.assertEqual(fetch.resolve("dual.test", port, True), ["192.0.2.1", "127.0.0.1"])
            data, _ = fetch.download_bytes(
                f"http://dual.test:{port}/data", timeout=1, max_bytes=100, allow_private=True
            )
            self.assertEqual(data, b"#EXTM3U\n")
            with self.assertRaisesRegex(fetch.FetchError, "non-public"):
                fetch.resolve("dual.test", port, False)

    def test_dead_addresses_share_the_total_deadline(self):
        # Many unreachable addresses, each given the full connect timeout, must not add up
        # past the whole-download deadline: every attempt gets at most the time left.
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"192.0.2.{i}", 80)) for i in range(1, 51)
        ]
        tried = []

        def dead(address, timeout):
            tried.append(timeout)
            time.sleep(min(timeout, 0.05))
            raise TimeoutError

        with (
            mock.patch("socket.getaddrinfo", return_value=infos),
            mock.patch("socket.create_connection", dead),
            mock.patch.object(fetch, "TOTAL_DEADLINE", 0.3),
            self.assertRaisesRegex(fetch.FetchError, "too long"),
        ):
            fetch.download_bytes("http://many.test/x", timeout=60, max_bytes=10, allow_private=True)
        self.assertLess(len(tried), 50)
        self.assertTrue(all(t <= 0.3 for t in tried), tried)

    def test_trickling_server_stopped_at_the_deadline(self):
        # A server that sends a byte every 50 ms never trips the 5 s socket timeout and
        # would hold one 64 KiB read (or the header parser) for minutes. Headers cut off by
        # the deadline must not pass as complete: a 304 would claim the cache is current.
        for path in ("/trickle-body", "/trickle-headers", "/trickle-304"):
            with self.subTest(path), mock.patch.object(fetch, "TOTAL_DEADLINE", 0.5):
                started = time.monotonic()
                with self.assertRaisesRegex(fetch.FetchError, "too long"):
                    self.get(path)
                self.assertLess(time.monotonic() - started, 2)

    def test_redirect_closes_its_connection_before_the_next_hop(self):
        # A single-threaded origin waits for the first keep-alive connection to close before
        # it accepts the redirected request: nothing may hold that connection open.
        srv = socket.create_server(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        seen = {}

        def origin():
            with srv:
                c1, _ = srv.accept()
                with c1:
                    c1.recv(65536)
                    c1.sendall(b"HTTP/1.1 301 Moved\r\nLocation: /x\r\nContent-Length: 0\r\n\r\n")
                    c1.settimeout(3)
                    try:
                        seen["closed"] = c1.recv(1) == b""
                    except OSError:
                        seen["closed"] = False
                c2, _ = srv.accept()
                with c2:
                    c2.recv(65536)
                    c2.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc")

        server = threading.Thread(target=origin, daemon=True)
        server.start()
        data, _ = fetch.download_bytes(
            f"http://127.0.0.1:{port}/", timeout=5, max_bytes=100, allow_private=True
        )
        server.join(5)
        self.assertEqual(data, b"abc")
        self.assertTrue(seen["closed"])

    def test_body_shorter_than_content_length_is_an_error(self):
        with self.assertRaisesRegex(fetch.FetchError, "ended early"):
            self.get("/short")

    def test_redirect_to_private_or_bad_scheme_refused(self):
        # allow_private only lets us reach the test server itself; the metadata IP is link-local
        # and is still refused because each hop is re-checked against the same rules.
        with self.assertRaises(fetch.FetchError):
            fetch.download_bytes(
                self.base + "/redirect-file", timeout=5, max_bytes=100, allow_private=True
            )
        # Hop 1 must pass the public check for hop 2 to be tested: treat loopback as public.
        real = fetch._is_public
        with (
            mock.patch.object(fetch, "_is_public", lambda ip: ip.is_loopback or real(ip)),
            self.assertRaisesRegex(
                fetch.FetchError, r"169\.254\.169\.254 resolves to a non-public"
            ),
        ):
            fetch.download_bytes(
                self.base + "/redirect-private", timeout=2, max_bytes=100, allow_private=False
            )

    def test_redirect_and_conditional(self):
        data, result = self.get("/redirect-ok")
        self.assertEqual(data, b"#EXTM3U\n")
        self.assertEqual(result.etag, '"v1"')
        self.assertTrue(result.url.endswith("/data"))
        with self.assertRaises(fetch.NotModified):
            self.get("/data", etag='"v1"')

    def test_size_limit_while_streaming(self):
        with self.assertRaises(fetch.SizeLimitExceeded):
            self.get("/big")

    def test_gzip_body(self):
        data, _ = self.get("/gz")
        self.assertEqual(data, b"#EXTM3U\nhello\n")

    def test_http_error(self):
        with self.assertRaisesRegex(fetch.FetchError, "HTTP 500"):
            self.get("/missing")

    def test_gzip_bomb(self):
        bomb = gzip.compress(b"\0" * (20 * 1024 * 1024))
        self.assertLess(len(bomb), 100_000)
        with self.assertRaises(fetch.SizeLimitExceeded):
            fetch.gunzip_bounded(bomb, 1024 * 1024)
        self.assertEqual(fetch.gunzip_bounded(gzip.compress(b"a") + gzip.compress(b"b"), 10), b"ab")

    def test_truncated_gzip_refused(self):
        data = gzip.compress(b"#EXTM3U\n" + b"#EXTINF:-1,Channel\nhttp://x/\n" * 2000)
        for cut in (data[: len(data) // 2], data[:-4], gzip.compress(b"a") + data[:20]):
            with self.subTest(len(cut)), self.assertRaisesRegex(fetch.FetchError, "Truncated"):
                fetch.gunzip_bounded(cut, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
