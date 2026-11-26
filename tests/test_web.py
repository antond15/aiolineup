import http.client
import io
import json
import logging
import socket
import threading
import unittest
from unittest import mock
from urllib.parse import quote

from aiolineup import web
from aiolineup.web import serve
from tests.helpers import TempEnv


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = TempEnv(PORT="0", HOST="127.0.0.1")
        cls.env.engine.build_playlist()
        cls.env.engine.build_epg()
        cls.env.engine.current_job = (
            None  # build_epg leaves its label; the worker normally clears it
        )
        cls.server = serve(cls.env.cfg, cls.env.engine)
        cls.port = cls.server.socket.getsockname()[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.log_stream = io.StringIO()
        handler = logging.StreamHandler(cls.log_stream)
        logging.getLogger("aiolineup.http").addHandler(handler)
        logging.getLogger("aiolineup.http").setLevel(logging.INFO)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.env.close()

    def request(self, method, path, body=None, headers=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault("Content-Type", "application/json")
            body = json.dumps(body)
        if cookie:
            hdrs["Cookie"] = cookie
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def login(self):
        resp, _ = self.request(
            "POST",
            "/api/login",
            {"username": "admin", "password": "correct-horse"},
            {"X-Requested-With": "aiolineup"},
        )
        self.assertEqual(resp.status, 200)
        cookie = resp.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        return cookie.split(";")[0]


class WebTests(WebCase):
    def test_health(self):
        resp, data = self.request("GET", "/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data)["status"], "ok")

    def test_api_requires_login(self):
        for method, path in (
            ("GET", "/api/overview"),
            ("GET", "/api/channels"),
            ("GET", "/api/settings"),
        ):
            resp, _ = self.request(method, path)
            self.assertEqual(resp.status, 401, path)

    def test_wrong_password(self):
        resp, _ = self.request(
            "POST",
            "/api/login",
            {"username": "admin", "password": "nope"},
            {"X-Requested-With": "aiolineup"},
        )
        self.assertEqual(resp.status, 401)

    def test_forged_cookie_rejected(self):
        resp, _ = self.request("GET", "/api/overview", cookie="aiolineup_session=9999999999.AAAA")
        self.assertEqual(resp.status, 401)

    def test_csrf_protection(self):
        cookie = self.login()
        resp, _ = self.request("POST", "/api/refresh", {}, cookie=cookie)
        self.assertEqual(resp.status, 403)
        resp, _ = self.request(
            "POST",
            "/api/refresh",
            {},
            {"X-Requested-With": "aiolineup", "Origin": "https://evil.example"},
            cookie,
        )
        self.assertEqual(resp.status, 403)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST",
            "/api/refresh",
            body="a=b",
            headers={
                "Cookie": cookie,
                "X-Requested-With": "aiolineup",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 202)  # refresh takes no body; header check is what matters
        resp, _ = self.request(
            "POST",
            "/api/sources",
            {"kind": "m3u", "name": "x", "url": "https://x/y"},
            {"X-Requested-With": "aiolineup", "Content-Type": "text/plain"},
            cookie,
        )
        self.assertEqual(resp.status, 415)

    def test_authenticated_api(self):
        cookie = self.login()
        h = {"X-Requested-With": "aiolineup"}
        resp, data = self.request("GET", "/api/overview", cookie=cookie)
        self.assertEqual(resp.status, 200)
        self.assertIn(
            "/feed/feedtoken_0123456789/playlist.m3u", json.loads(data)["feeds"]["playlist"]
        )
        resp, data = self.request("GET", "/api/channels?q=anything&limit=5", cookie=cookie)
        self.assertEqual(json.loads(data)["total"], 0)
        # Input validation
        for bad in (
            {"kind": "m3u", "name": "x", "url": "file:///etc/passwd"},
            {"kind": "m3u", "name": "x", "url": "https://user:pw@host/x"},
            {"kind": "bogus", "name": "x", "url": "https://host/x"},
            {"kind": "m3u", "name": "", "url": "https://host/x"},
        ):
            resp, _ = self.request("POST", "/api/sources", bad, h, cookie)
            self.assertEqual(resp.status, 400, bad)
        resp, _ = self.request("PUT", "/api/settings", {"bogus": 1}, h, cookie)
        self.assertEqual(resp.status, 400)
        resp, data = self.request("PUT", "/api/settings", {"epg_days": 3}, h, cookie)
        self.assertEqual(json.loads(data)["settings"]["epg_days"], 3)

    def test_epg_search_ignores_accents_and_reports_sources(self):
        db = self.env.db
        with db.conn() as c:
            c.execute(
                "INSERT INTO sources (id, kind, name, url, priority, created_at) VALUES (90, 'xmltv', 'CZ', 'https://g/cz', 5, 0)"
            )
            c.execute(
                "INSERT INTO epg_channels (source_id, epg_id, epg_id_lc, display_name, programmes)"
                " VALUES (90, 'ČT1.cz', 'ct1.cz', 'CZ - ČT1', 12)"
            )
        try:
            cookie = self.login()
            for q in ("ct1", "ČT1", "CZ - CT1", "čt"):
                resp, data = self.request(
                    "GET",
                    f"/api/epg/search?q={__import__('urllib.parse').parse.quote(q)}",
                    cookie=cookie,
                )
                self.assertEqual([r["epg_id"] for r in json.loads(data)["results"]], ["ČT1.cz"], q)
            self.assertEqual(json.loads(data)["results"][0]["source_id"], 90)
            self.assertIsNone(json.loads(data)["results"][0]["now"])
            resp, data = self.request("GET", "/api/epg", cookie=cookie)
            status = {s["name"]: s for s in json.loads(data)["guide_sources"]}
            self.assertEqual((status["CZ"]["channels"], status["CZ"]["with_programmes"]), (1, 1))

            # A mapping remembers the guide it was picked from.
            h = {"X-Requested-With": "aiolineup"}
            key = "id:ct1.cz"
            with db.conn() as c:
                c.execute("INSERT INTO selections VALUES (?, 'ČT1', 0, 1)", (key,))
            for bad in (999, "90", True):
                body = {"key": key, "epg_id": "ČT1.cz", "source_id": bad}
                resp, _ = self.request("PUT", "/api/epg/mapping", body, h, cookie)
                self.assertEqual(resp.status, 400, bad)
            body = {"key": key, "epg_id": "ČT1.cz", "source_id": 90}
            resp, _ = self.request("PUT", "/api/epg/mapping", body, h, cookie)
            self.assertEqual(resp.status, 200)
            rows = json.loads(self.request("GET", "/api/epg", cookie=cookie)[1])["channels"]
            row = next(r for r in rows if r["key"] == key)
            self.assertEqual((row["mapping"], row["mapping_source"]), ("ČT1.cz", "CZ"))
        finally:
            with db.conn() as c:
                c.execute("DELETE FROM sources WHERE id = 90")
                c.execute("DELETE FROM epg_mappings WHERE channel_key = 'id:ct1.cz'")
                c.execute("DELETE FROM selections WHERE channel_key = 'id:ct1.cz'")

    def test_sources_report_selected_channels_in_use(self):
        db = self.env.db
        coverage = db.get_kv("epg_coverage", {})
        with db.conn() as c:
            c.execute(
                "INSERT INTO sources (id, kind, name, url, enabled, created_at) VALUES"
                " (91, 'm3u', 'A', 'https://p/a', 1, 0), (92, 'm3u', 'B', 'https://p/b', 0, 0),"
                " (93, 'xmltv', 'G', 'https://g/g', 1, 0)"
            )
            for sid, pos, key in ((91, 0, "id:u1"), (91, 1, "id:u1"), (91, 2, "id:u2"),
                                  (91, 3, "id:other"), (92, 0, "id:u1")):  # fmt: skip
                c.execute(
                    "INSERT INTO streams VALUES (?, ?, ?, 't', 'https://s/x', '-1', '{}', '[]')",
                    (sid, pos, key),
                )
            c.execute("INSERT INTO selections VALUES ('id:u1', 'U1', 0, 1), ('id:u2', 'U2', 0, 2)")
        # A stale coverage entry for a channel no longer selected is not counted.
        db.set_kv(
            "epg_coverage",
            {
                "id:u1": {"source_id": 93, "epg_id": "U1"},
                "id:gone": {"source_id": 93, "epg_id": "X"},
            },
        )
        try:
            cookie = self.login()
            data = json.loads(self.request("GET", "/api/sources", cookie=cookie)[1])
            used = {s["id"]: s["channels_used"] for s in data["sources"]}
            self.assertEqual((used[91], used[92], used[93]), (2, None, 1))
        finally:
            db.set_kv("epg_coverage", coverage)
            with db.conn() as c:
                c.execute("DELETE FROM sources WHERE id IN (91, 92, 93)")
                c.execute("DELETE FROM selections WHERE channel_key IN ('id:u1', 'id:u2')")

    def test_feeds_need_token_and_are_redacted_in_logs(self):
        resp, _ = self.request("GET", "/feed/wrong-token-0000000/playlist.m3u")
        self.assertEqual(resp.status, 404)
        resp, data = self.request("GET", "/feed/feedtoken_0123456789/playlist.m3u")
        self.assertEqual(resp.status, 200)
        self.assertTrue(data.startswith(b"#EXTM3U"))
        etag = resp.getheader("ETag")
        resp, _ = self.request(
            "GET", "/feed/feedtoken_0123456789/playlist.m3u", headers={"If-None-Match": etag}
        )
        self.assertEqual(resp.status, 304)
        resp, data = self.request(
            "GET", "/feed/feedtoken_0123456789/epg.xml", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(resp.getheader("Content-Encoding"), "gzip")
        self.assertEqual(data[:2], b"\x1f\x8b")
        resp, data = self.request("GET", "/feed/feedtoken_0123456789/epg.xml")
        self.assertIsNone(resp.getheader("Content-Encoding"))
        self.assertIn(b"<tv", data)
        self.assertNotIn("feedtoken_0123456789", self.log_stream.getvalue())
        self.assertIn("/feed/***/", self.log_stream.getvalue())

    def test_encoded_feed_paths_refused_and_redacted(self):
        token = "feedtoken_0123456789"
        encoded = "".join(f"%{ord(ch):02x}" for ch in token)
        for path in (
            f"/feed/{token}%2fplaylist.m3u",
            f"/%66eed/{token}/playlist.m3u",
            f"/feed/{encoded}/playlist.m3u",
            f"/feed/{encoded.replace('%', '%25')}/epg.xml",
            f"/FEED/{token}",
        ):
            with self.subTest(path):
                resp, _ = self.request("GET", path)
                self.assertEqual(resp.status, 404)
        self.assertNotIn(token, self.log_stream.getvalue())

    def test_negative_content_length_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/login")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("X-Requested-With", "aiolineup")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        resp = conn.getresponse()  # before the fix this hung until the timeout
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 400)

    def test_static_and_traversal(self):
        resp, data = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy"))
        page = data.decode()
        self.assertRegex(page, r'/static/app\.css\?v=[0-9a-f]{10}"')
        self.assertRegex(page, r'/static/app\.js\?v=[0-9a-f]{10}"')
        self.assertRegex(page, r'/static/icon\.svg\?v=[0-9a-f]{10}"')
        resp, _ = self.request("GET", "/static/app.css?v=abc")
        self.assertEqual(resp.status, 200)
        for path in (
            "/static/../config.py",
            "/static/..%2fconfig.py",
            "/static/app.py",
            "/etc/passwd",
        ):
            resp, _ = self.request("GET", path)
            self.assertEqual(resp.status, 404, path)


H = {"X-Requested-With": "aiolineup"}


class WebApiTests(WebCase):
    """Selection, sources and limits, each test on its own data."""

    def setUp(self):
        self.cookie = self.login()
        self.env.engine._pending_epg_at = None  # other tests queue rebuilds on the shared engine
        self.addCleanup(self.reset)
        db = self.env.db
        with db.conn() as c:
            c.execute(
                "INSERT INTO sources (id, kind, name, url, priority, created_at) VALUES"
                " (91, 'm3u', 'A', 'https://p/a', 10, 0), (92, 'm3u', 'B', 'https://p/b', 20, 0)"
            )
            rows = (
                (91, 0, "id:a1", "Alpha One", "https://s/a1", "Sports"),
                (91, 1, "id:a2", "Alpha Two", "https://s/a2", "News"),
                (91, 2, "id:b1", "Beta", "https://s/b1", "Sports"),
            )
            for sid, pos, key, title, url, group in rows:
                c.execute(
                    "INSERT INTO streams VALUES (?, ?, ?, ?, ?, '-1', ?, '[]')",
                    (sid, pos, key, title, url, json.dumps({"group-title": group})),
                )
        self.env.engine.invalidate_catalog()

    def reset(self):
        with self.env.db.conn() as c:
            c.execute("DELETE FROM sources")
            c.execute("DELETE FROM selections")
            c.execute("DELETE FROM stream_prefs")
            c.execute("DELETE FROM channel_edits")
            c.execute("DELETE FROM category_overrides")
        self.env.engine.invalidate_catalog()
        self.env.engine._pending_epg_at = None
        for p in self.env.cfg.cache_dir.iterdir():
            p.unlink()

    def call(self, method, path, body=None):
        resp, data = self.request(method, path, body, H, self.cookie)
        return resp.status, json.loads(data) if data else None

    def selected(self):
        return {r["channel_key"] for r in self.env.db.query("SELECT channel_key FROM selections")}

    def test_bulk_select_and_deselect_follow_the_search(self):
        status, out = self.call(
            "POST", "/api/selection", {"filter": {"q": "alpha"}, "action": "select"}
        )
        self.assertEqual((status, out["changed"]), (200, 2))
        self.assertEqual(self.selected(), {"id:a1", "id:a2"})
        self.call("POST", "/api/selection", {"filter": {"q": "beta"}, "action": "select"})
        # Deselecting a search touches only what it matches.
        _, out = self.call(
            "POST", "/api/selection", {"filter": {"q": "alpha one"}, "action": "deselect"}
        )
        self.assertEqual(out["changed"], 1)
        self.assertEqual(self.selected(), {"id:a2", "id:b1"})

    def item(self, key: str, query: str = "") -> dict:
        _, data = self.call("GET", f"/api/channels{query}")
        return next(i for i in data["items"] if i["key"] == key)

    def test_category_edit_changes_items_filter_and_facets(self):
        # Works for an unselected channel too.
        status, out = self.call(
            "PUT", "/api/channel/edit", {"key": "id:a1", "categories": ["  news ", "Kids"]}
        )
        self.assertEqual(status, 200)
        # The existing spelling is taken; the list keeps the order given.
        self.assertEqual(
            (out["categories"], out["feed_categories"], out["category_edited"]),
            (["News", "Kids"], ["News", "Kids"], True),
        )
        a1 = self.item("id:a1")
        self.assertEqual(
            (a1["categories"], a1["feed_categories"], a1["category_edited"]),
            (["News", "Kids"], ["News", "Kids"], True),
        )
        b1 = self.item("id:b1")
        self.assertEqual((b1["feed_categories"], b1["category_edited"]), (["Sports"], False))
        _, data = self.call("GET", "/api/channels")
        self.assertEqual(dict(data["facets"]["categories"]), {"News": 2, "Sports": 1, "Kids": 1})
        # The source category the own list hides no longer matches.
        _, data = self.call("GET", "/api/channels?category=Sports")
        self.assertEqual([i["key"] for i in data["items"]], ["id:b1"])
        _, data = self.call("GET", "/api/channels?category=Kids")
        self.assertEqual([i["key"] for i in data["items"]], ["id:a1"])
        # Reset.
        status, out = self.call("PUT", "/api/channel/edit", {"key": "id:a1", "categories": None})
        self.assertEqual(
            (status, out["categories"], out["category_edited"]), (200, ["Sports"], False)
        )
        _, data = self.call("GET", "/api/channels?category=Sports")
        self.assertEqual([i["key"] for i in data["items"]], ["id:a1", "id:b1"])

    def test_category_edit_validation(self):
        bad = [
            [],
            [""],
            ["   "],
            ["x" * 61],
            ["a;b"],
            ["a|b"],
            ['a"b'],
            ["a\nb"],
            ["a\x00b"],
            ["a\x7fb"],
            [5],
            ["ok", None],
            "News",  # text, not a list
            5,
            {"a": 1},
            [f"C{i}" for i in range(9)],  # more than 8
            ["News", "NEWS"],  # a repeat ignoring case
            ["Kids", " kids "],
        ]
        for value in bad:
            status, out = self.call(
                "PUT", "/api/channel/edit", {"key": "id:a1", "categories": value}
            )
            self.assertEqual(status, 400, repr(value))
        self.assertFalse(self.item("id:a1")["category_edited"])  # nothing was stored
        status, _ = self.call("PUT", "/api/channel/edit", {"key": "id:zzz", "categories": ["X"]})
        self.assertEqual(status, 404)
        # An absent field is an error, not a reset of a saved list; the old route is gone.
        self.call("PUT", "/api/channel/edit", {"key": "id:a1", "categories": ["Kept"]})
        status, _ = self.call("PUT", "/api/channel/edit", {"key": "id:a1"})
        self.assertEqual(status, 400)
        status, _ = self.call("PUT", "/api/channel/category", {"key": "id:a1", "category": "X"})
        self.assertEqual(status, 404)
        status, _ = self.call("PUT", "/api/channel/categories", {"key": "id:a1", "categories": []})
        self.assertEqual(status, 404)
        self.assertEqual(self.item("id:a1")["categories"], ["Kept"])
        eight = ["x" * 60, *(f"C{i}" for i in range(7))]
        status, out = self.call("PUT", "/api/channel/edit", {"key": "id:a1", "categories": eight})
        self.assertEqual((status, out["categories"]), (200, eight))
        # Needs the same CSRF header as every mutating request.
        resp, _ = self.request(
            "PUT", "/api/channel/edit", {"key": "id:a1", "categories": ["X"]}, {}, self.cookie
        )
        self.assertIn(resp.status, (400, 403))

    def test_bulk_deselect_treats_missing_channels_as_name_only(self):
        with self.env.db.conn() as c:
            c.execute("INSERT INTO selections VALUES ('id:gone', 'Alpha Gone', 0, 901)")
            c.execute("INSERT INTO selections VALUES ('id:a1', 'Alpha One', 0, 902)")
        sel = {"status": "selected"}
        # A search that does not match the missing channel's name leaves it selected.
        self.call("POST", "/api/selection", {"filter": {**sel, "q": "beta"}, "action": "deselect"})
        self.assertEqual(self.selected(), {"id:gone", "id:a1"})
        # Any other filter excludes missing channels: they have no category to match.
        self.call(
            "POST",
            "/api/selection",
            {"filter": {**sel, "category": "Sports"}, "action": "deselect"},
        )
        self.assertEqual(self.selected(), {"id:gone"})
        _, out = self.call("GET", "/api/channels?status=selected&category=Sports")
        self.assertEqual(out["missing"], [])
        _, out = self.call("GET", "/api/channels?status=selected&q=gone")
        self.assertEqual([m["key"] for m in out["missing"]], ["id:gone"])
        # The search matches it, so a bulk deselect of that search removes it too.
        self.call("POST", "/api/selection", {"filter": {**sel, "q": "gone"}, "action": "deselect"})
        self.assertEqual(self.selected(), set())

    def test_bulk_select_numbers_alphabetically_and_lists_by_number(self):
        self.call("POST", "/api/selection", {"select": ["id:b1"]})
        self.call("POST", "/api/selection", {"filter": {}, "action": "select"})
        self.assertEqual(self.env.engine.selection_numbers(), {"id:b1": 1, "id:a1": 2, "id:a2": 3})
        _, out = self.call("PUT", "/api/channel/number", {"key": "id:a2", "number": 50})
        self.assertEqual(out, {"ok": True, "number": 50})
        _, out = self.call("GET", "/api/channels?status=selected&sort=number")
        self.assertEqual(
            [(i["key"], i["number"]) for i in out["items"]],
            [("id:b1", 1), ("id:a1", 2), ("id:a2", 50)],
        )
        _, out = self.call("GET", "/api/channels")  # name order stays the default
        self.assertEqual([i["key"] for i in out["items"]], ["id:a1", "id:a2", "id:b1"])

    def test_number_validation(self):
        self.call("POST", "/api/selection", {"select": ["id:a1", "id:b1"]})
        for bad in (0, 10000, "3", 2.0, True, None):
            status, out = self.call("PUT", "/api/channel/number", {"key": "id:b1", "number": bad})
            self.assertEqual(status, 400, bad)
        status, out = self.call("PUT", "/api/channel/number", {"key": "id:b1", "number": 1})
        self.assertEqual((status, out["error"]), (409, "Number 1 is used by Alpha One."))
        status, _ = self.call("PUT", "/api/channel/number", {"key": "id:a2", "number": 9})
        self.assertEqual(status, 404)
        self.assertEqual(self.env.engine.selection_numbers(), {"id:a1": 1, "id:b1": 2})

    def test_stream_choices_round_trip(self):
        with self.env.db.conn() as c:
            c.execute(
                "INSERT INTO streams VALUES (92, 0, 'id:a1', 'Alpha One', 'https://s/a1b', '-1',"
                " '{}', '[\"#EXTVLCOPT:http-referrer=https://r/\"]')"
            )
        self.env.engine.invalidate_catalog()
        self.call("POST", "/api/selection", {"select": ["id:a1"]})
        _, out = self.call("GET", "/api/channel?key=id:a1")
        self.assertEqual(
            [(s["source"], s["url"], s["on"]) for s in out["feed_streams"]],
            [("A", "https://s/a1", True), ("B", "https://s/a1b", True)],
        )
        self.assertFalse(out["streams_edited"])
        first, second = (s["sig"] for s in out["feed_streams"])
        body = {
            "key": "id:a1",
            "streams": [{"sig": second, "on": True}, {"sig": first, "on": False}],
        }
        self.assertEqual(self.call("PUT", "/api/channel/streams", body)[0], 200)
        _, out = self.call("GET", "/api/channel?key=id:a1")
        self.assertEqual(
            [(s["url"], s["on"]) for s in out["feed_streams"]],
            [("https://s/a1b", True), ("https://s/a1", False)],
        )
        self.assertTrue(out["streams_edited"])
        self.assertEqual(len(out["streams"]), 2)  # the old list the UI reads is unchanged
        all_off = {
            "key": "id:a1",
            "streams": [{"sig": first, "on": False}, {"sig": second, "on": False}],
        }
        status, out = self.call("PUT", "/api/channel/streams", all_off)
        self.assertEqual((status, out["error"]), (400, "At least one stream must stay on."))
        for bad in (
            {"key": "id:a1"},  # absent must not reset
            {"key": "id:a1", "streams": "x"},
            {"key": "id:a1", "streams": [{"sig": first}]},
            {"key": "id:a1", "streams": [{"sig": "x", "on": True}]},
        ):
            self.assertEqual(self.call("PUT", "/api/channel/streams", bad)[0], 400, bad)
        too_many = {"key": "id:a1", "streams": [{"sig": first, "on": True}] * 2001}
        status, out = self.call("PUT", "/api/channel/streams", too_many)
        self.assertEqual((status, out["error"]), (400, "At most 2000 streams."))
        status, _ = self.call("PUT", "/api/channel/streams", {"key": "id:b1", "streams": None})
        self.assertEqual(status, 404)
        status, out = self.call("PUT", "/api/channel/streams", {"key": "id:a1", "streams": None})
        self.assertEqual((status, out["streams_edited"]), (200, False))
        self.assertEqual(self.env.db.one("SELECT count(*) FROM stream_prefs")[0], 0)

    def test_own_name_and_logo(self):
        for bad in (
            {"key": "id:a1"},
            {"key": "id:a1", "name": "x" * 201},
            {"key": "id:a1", "name": "A\tB"},
            {"key": "id:a1", "name": 5},
            {"key": "id:a1", "logo": "ftp://l/x.png"},
            {"key": "id:a1", "logo": 'https://l/"x.png'},
            {"key": "id:a1", "logo": "https://l/" + "x" * 2048},
            # XML 1.0 forbids these: one in a name or logo would break the whole guide.
            {"key": "id:a1", "name": "A\U0000fffeB"},
            {"key": "id:a1", "name": "A\U0000ffff"},
            {"key": "id:a1", "logo": "https://l/x\U0000fffe.png"},
            {"key": "id:a1", "logo": "https://l/x\x7f.png"},
        ):
            self.assertEqual(self.call("PUT", "/api/channel/edit", bad)[0], 400, bad)
        self.assertEqual(
            self.call("PUT", "/api/channel/edit", {"key": "id:zz", "name": "Z"})[0], 404
        )
        body = {"key": "id:a1", "name": "  Uno  ", "logo": "https://l/uno.png"}
        status, out = self.call("PUT", "/api/channel/edit", body)
        self.assertEqual(
            (status, out),
            (200, {"ok": True, "name": "Uno", "name_edited": True,
                   "logo": "https://l/uno.png", "logo_edited": True}),
        )  # fmt: skip
        for q in ("uno", "alpha one"):  # the own name and the source name both match
            _, out = self.call("GET", f"/api/channels?q={q.replace(' ', '+')}")
            self.assertEqual([i["key"] for i in out["items"]], ["id:a1"], q)
        item = out["items"][0]
        self.assertEqual(
            (item["name"], item["source_name"], item["logo"], item["source_logo"]),
            ("Uno", "Alpha One", "https://l/uno.png", ""),
        )
        _, out = self.call("PUT", "/api/channel/edit", {"key": "id:a1", "name": ""})
        self.assertEqual(
            (out["name"], out["name_edited"], out["logo_edited"]), ("Alpha One", False, True)
        )

    def test_own_logo_must_be_https(self):
        for logo in ("http://l/x.png", "HTTP://l/x.png"):
            status, _ = self.call("PUT", "/api/channel/edit", {"key": "id:a1", "logo": logo})
            self.assertEqual(status, 400, logo)
        self.assertFalse(self.item("id:a1")["logo_edited"])
        # A saved http override (from before) is kept when sent back unchanged, refused when
        # edited to another http URL, and can be replaced or cleared.
        with self.env.db.conn() as c:
            c.execute("INSERT INTO channel_edits VALUES ('id:a1', NULL, 'http://l/old.png')")
        status, out = self.call(
            "PUT", "/api/channel/edit", {"key": "id:a1", "name": "N", "logo": "http://l/old.png"}
        )
        self.assertEqual((status, out["logo"]), (200, "http://l/old.png"))
        status, _ = self.call(
            "PUT", "/api/channel/edit", {"key": "id:a1", "logo": "http://l/new.png"}
        )
        self.assertEqual(status, 400)
        status, out = self.call("PUT", "/api/channel/edit", {"key": "id:a1", "logo": ""})
        self.assertEqual((status, out["logo_edited"]), (200, False))

    def test_edit_saves_name_logo_and_categories_together(self):
        body = {"key": "id:a1", "name": "Uno", "logo": "https://l/u.png", "categories": ["Kids"]}
        status, out = self.call("PUT", "/api/channel/edit", body)
        self.assertEqual(status, 200)
        self.assertEqual(
            (out["name"], out["logo"], out["categories"], out["category_edited"]),
            ("Uno", "https://l/u.png", ["Kids"], True),
        )
        item = self.item("id:a1")
        self.assertEqual(
            (item["name"], item["logo"], item["categories"]), ("Uno", "https://l/u.png", ["Kids"])
        )
        # A refused part writes nothing: the bad category list keeps the name as it was.
        status, _ = self.call(
            "PUT", "/api/channel/edit", {"key": "id:a1", "name": "Dos", "categories": ["A", "a"]}
        )
        self.assertEqual(status, 400)
        item = self.item("id:a1")
        self.assertEqual((item["name"], item["categories"]), ("Uno", ["Kids"]))
        # Fields left out are kept; null categories reset only the list.
        status, out = self.call("PUT", "/api/channel/edit", {"key": "id:a1", "categories": None})
        self.assertEqual((status, out["name"], out["category_edited"]), (200, "Uno", False))

    def test_missing_channel_shows_and_matches_its_own_name(self):
        with self.env.db.conn() as c:
            c.execute("INSERT INTO selections VALUES ('id:gone', 'Gone', 0, 4)")
            c.execute("INSERT INTO channel_edits VALUES ('id:gone', 'Mine', NULL)")
        _, out = self.call("GET", "/api/channels?status=selected&q=mine")
        self.assertEqual(out["missing"], [{"key": "id:gone", "name": "Mine", "number": 4}])
        _, out = self.call("GET", "/api/channels?status=selected&q=gone")
        self.assertEqual([m["key"] for m in out["missing"]], ["id:gone"])
        status, _ = self.call("PUT", "/api/channel/number", {"key": "id:gone", "number": 5})
        self.assertEqual(status, 200)
        # The channel panel sees the stored edits, so the editor never shows them blank.
        _, out = self.call(
            "PUT", "/api/channel/edit", {"key": "id:gone", "logo": "https://l/g.png"}
        )
        self.assertEqual((out["name"], out["logo"]), ("Mine", "https://l/g.png"))
        _, out = self.call("GET", "/api/channel?key=id:gone")
        self.assertEqual(
            {k: out[k] for k in ("number", "name", "source_name", "name_edited", "logo")},
            {
                "number": 5,
                "name": "Mine",
                "source_name": "Gone",
                "name_edited": True,
                "logo": "https://l/g.png",
            },
        )
        _, out = self.call("PUT", "/api/channel/edit", {"key": "id:gone", "name": None})
        self.assertEqual((out["name"], out["name_edited"]), ("Gone", False))

    def test_feed_streams_mark_a_url_aiostreams_already_has(self):
        # Same URL, other headers: two streams here, one in AIOStreams (it keeps one per URL).
        with self.env.db.conn() as c:
            c.execute(
                "INSERT INTO streams VALUES (92, 0, 'id:a1', 'Alpha One', 'https://s/a1', '-1',"
                " '{}', '[\"#EXTVLCOPT:http-referrer=https://r/\"]')"
            )
        self.env.engine.invalidate_catalog()
        self.call("POST", "/api/selection", {"select": ["id:a1"]})
        _, out = self.call("GET", "/api/channel?key=id:a1")
        self.assertEqual([s["url_repeat"] for s in out["feed_streams"]], [False, True])
        first, second = (s["sig"] for s in out["feed_streams"])
        body = {
            "key": "id:a1",
            "streams": [{"sig": first, "on": False}, {"sig": second, "on": True}],
        }
        self.call("PUT", "/api/channel/streams", body)
        _, out = self.call("GET", "/api/channel?key=id:a1")
        self.assertEqual([s["url_repeat"] for s in out["feed_streams"]], [False, False])

    def test_drawer_duplicates_ignore_disabled_sources(self):
        with self.env.db.conn() as c:
            c.execute("UPDATE sources SET enabled = 0 WHERE id = 92")
            c.execute(
                "INSERT INTO streams VALUES (92, 0, 'id:a1', 'Alpha One', 'https://s/a1', '-1', '{}', '[]')"
            )
            c.execute(
                "INSERT INTO streams VALUES (91, 3, 'id:a1', 'Alpha One', 'https://s/a1', '-1', '{}', '[]')"
            )
        _, out = self.call("GET", "/api/channel?key=id:a1")
        # The enabled copy (priority 10) is first; the same URL later in its own source is a
        # duplicate, the disabled source's copy is neither a duplicate nor does it mark one.
        flags = [(s["source"], s["enabled"], s["duplicate"]) for s in out["streams"]]
        self.assertEqual(flags, [("A", True, False), ("A", True, True), ("B", False, False)])
        with self.env.db.conn() as c:
            c.execute("UPDATE sources SET enabled = 1 WHERE id = 92")
            c.execute("UPDATE sources SET enabled = 0, priority = 5 WHERE id = 91")
            c.execute("UPDATE sources SET priority = 6 WHERE id = 92")
        _, out = self.call("GET", "/api/channel?key=id:a1")
        self.assertEqual(
            [(s["source"], s["duplicate"]) for s in out["streams"]],
            [("A", False), ("A", False), ("B", False)],
        )

    def test_source_update_and_delete(self):
        status, _ = self.call("PUT", "/api/sources/91", {"name": "Renamed", "priority": 3})
        self.assertEqual(status, 200)
        row = self.env.db.one("SELECT name, priority FROM sources WHERE id = 91")
        self.assertEqual((row["name"], row["priority"]), ("Renamed", 3))
        self.assertEqual(self.call("PUT", "/api/sources/91", {"kind": "xmltv"})[0], 400)
        self.assertEqual(self.call("PUT", "/api/sources/999", {"name": "x"})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/sources/999")[0], 404)
        engine = self.env.engine
        engine.raw_path(91).write_bytes(b"x")
        self.assertEqual(self.call("DELETE", "/api/sources/91")[0], 200)
        self.assertFalse(engine.raw_path(91).exists())
        self.assertEqual(self.env.db.one("SELECT count(*) n FROM streams")["n"], 0)
        self.assertNotIn("id:a1", engine.catalog())

    def test_epg_search_uses_enabled_guides_and_two_characters(self):
        with self.env.db.conn() as c:
            c.execute(
                "INSERT INTO sources (id, kind, name, url, enabled, created_at) VALUES"
                " (93, 'xmltv', 'On', 'https://g/on', 1, 0), (94, 'xmltv', 'Off', 'https://g/off', 0, 0)"
            )
            for sid in (93, 94):
                c.execute(
                    "INSERT INTO epg_channels (source_id, epg_id, epg_id_lc, display_name, programmes)"
                    " VALUES (?, 'Foo.us', 'foo.us', 'Foo', 3)",
                    (sid,),
                )
        _, out = self.call("GET", "/api/epg/search?q=foo")
        self.assertEqual([r["source_id"] for r in out["results"]], [93])
        for q in ("f", "-", " . ", "%", "_"):
            self.assertEqual(self.call("GET", "/api/epg/search?q=" + quote(q))[1]["results"], [], q)

    def test_epg_reports_a_pending_rebuild(self):
        _, out = self.call("GET", "/api/epg")
        self.assertFalse(out["epg_pending"])
        self.env.engine.request_epg(delay=3600)
        _, out = self.call("GET", "/api/epg")
        self.assertTrue(out["epg_pending"])

    def test_login_is_rate_limited(self):
        app = self.server.app
        self.addCleanup(app._failures.clear)
        with mock.patch.object(web.time, "sleep"):
            for _ in range(web.LOGIN_MAX_FAILURES):
                resp, _ = self.request(
                    "POST", "/api/login", {"username": "admin", "password": "no"}, H
                )
                self.assertEqual(resp.status, 401)
            # Locked out: even the right password is refused.
            resp, _ = self.request(
                "POST", "/api/login", {"username": "admin", "password": "correct-horse"}, H
            )
            self.assertEqual(resp.status, 429)

    def test_oversized_body_refused_before_reading(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/login")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("X-Requested-With", "aiolineup")
        conn.putheader("Content-Length", str(web.MAX_BODY + 1))
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 413)

    def test_feed_resumes_from_the_sent_offset_when_sendfile_fails(self):
        path = self.env.engine.playlist_path
        self.env.engine.build_playlist()
        path.write_bytes(b"#EXTM3U\n" + b"#EXTINF:-1,x\nhttp://s/x\n" * 2000)
        expected = path.read_bytes()

        def partial_sendfile(sock, fh, *args, **kwargs):
            sock.sendall(fh.read(1000))  # sent, offset advanced, then the call fails
            raise OSError("sendfile failed")

        with mock.patch.object(socket.socket, "sendfile", partial_sendfile):
            resp, data = self.request("GET", "/feed/feedtoken_0123456789/playlist.m3u")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, expected)

    def test_request_threads_close_their_database_connection(self):
        closed = []
        real = self.env.db.close
        with mock.patch.object(self.env.db, "close", lambda: (closed.append(1), real())[1]):
            self.request("GET", "/health")
            for _ in range(50):
                if closed:
                    break
                threading.Event().wait(0.05)
        self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main()
