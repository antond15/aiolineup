import http.client
import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aiolineup import __version__, backup
from aiolineup.web import serve
from tests.helpers import PLAYLIST_A, PLAYLIST_B, TempEnv, guide, write_gz
from tests.test_engine import FakeUpstream, xt

HEADERS = {"X-Requested-With": "aiolineup"}
GUIDE_URL = "https://g/3.xml.gz"

# Tables and kv keys that hold the admin's work, compared after a round trip.
USER_TABLES = {
    "sources": "id, kind, name, url, enabled, priority, created_at",
    "selections": "channel_key, name, added_at, number",
    "category_overrides": "channel_key, position, category",
    "channel_edits": "channel_key, name, logo",
    "stream_prefs": "channel_key, signature, position, enabled",
    "epg_mappings": "channel_key, epg_id, source_id",
}


def populate(env: TempEnv, up: FakeUpstream) -> None:
    """Two playlists and a guide, refreshed, with every kind of the admin's work saved."""
    db, engine = env.db, env.engine
    now = time.time()
    with db.conn() as c:
        for row in (
            (1, "m3u", "A", "https://a/a.m3u?user=me&pass=secret", 10, 1),
            (2, "m3u", "B", "https://b/b.m3u", 20, 1),
            (3, "xmltv", "G3", GUIDE_URL, 10, 1),
            (4, "m3u", "Off", "https://off/o.m3u", 30, 0),
        ):
            c.execute(
                "INSERT INTO sources (id, kind, name, url, priority, enabled, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*row, now),
            )
    up.content["https://a/a.m3u?user=me&pass=secret"] = PLAYLIST_A.encode()
    up.content["https://b/b.m3u"] = PLAYLIST_B.encode()
    path = env.dir / "g3.xml.gz"
    write_gz(
        path,
        guide(
            {
                "RedBullTV.at": [(xt(1), xt(2), "RB")],
                "NBATV.us": [(xt(1), xt(2), "Game")],
                "TG.custom": [(xt(3), xt(4), "Top Gear ep")],
            }
        ),
    )
    up.content[GUIDE_URL] = path.read_bytes()
    db.set_kv("settings", {"enrich_languages": False, "epg_days": 3, "playlist_epg_header": True})
    engine._execute({1, 2, 3}, True, False)
    engine.set_selection(["id:redbulltv.at@sd", "id:topgear.uk@sd", "id:nbatv.us@sd"], [])
    engine.set_number("id:nbatv.us@sd", 40)
    engine.set_categories("id:topgear.uk@sd", ["Cars", "Fun"])
    engine.set_edit("id:redbulltv.at@sd", name="Red Bull Own", logo="https://example.org/own.png")
    sigs = [sig for sig, _, _ in engine.channel_streams("id:redbulltv.at@sd")]
    engine.set_streams("id:redbulltv.at@sd", [(sigs[1], True), (sigs[0], False)])
    with db.conn() as c:
        c.execute(
            "INSERT INTO epg_mappings (channel_key, epg_id, source_id)"
            " VALUES ('id:topgear.uk@sd', 'TG.custom', 3)"
        )
        c.execute("INSERT INTO epg_mappings (channel_key, epg_id) VALUES ('id:nbatv.us@sd', '')")
    engine._execute(set(), True, False)
    engine.build_epg()


def user_data(env: TempEnv) -> dict:
    out = {
        name: [tuple(r) for r in env.db.query(f"SELECT {cols} FROM {name} ORDER BY 1, 2")]
        for name, cols in USER_TABLES.items()
    }
    out["settings"] = env.db.settings()
    return out


def everything(env: TempEnv) -> dict:
    """Every table and kv row plus the cache and output files: a check must leave all as is."""
    tables = ("streams", "epg_channels", "epg_schedule", "meta_languages", "kv")
    out = {t: [tuple(r) for r in env.db.query(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in tables}
    out["sources"] = [tuple(r) for r in env.db.query("SELECT * FROM sources ORDER BY id")]
    out.update(user_data(env))
    out["files"] = sorted(
        (str(p.relative_to(env.dir)), p.stat().st_size)
        for p in env.dir.rglob("*")
        if p.is_file() and p.suffix not in (".db-wal", ".db-shm", ".db")
    )
    return out


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream().__enter__()
        self.env = TempEnv(PORT="0", HOST="127.0.0.1", PUBLIC_URL="https://lineup.example")
        populate(self.env, self.up)
        self.servers: list = []
        self.addCleanup(self.cleanup)
        self.port, self.cookie = self.start(self.env)

    def cleanup(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.env.close()
        self.up.__exit__()

    def start(self, env: TempEnv):
        server = serve(env.cfg, env.engine)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        port = server.socket.getsockname()[1]
        resp, _ = self.raw(
            port,
            "POST",
            "/api/login",
            json.dumps({"username": "admin", "password": "correct-horse"}).encode(),
            HEADERS,
        )
        self.assertEqual(resp.status, 200)
        return port, resp.getheader("Set-Cookie").split(";")[0]

    def raw(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def call(self, method, path, body=None, port=None, cookie=True, headers=HEADERS):
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = self.cookie
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return self.raw(port or self.port, method, path, body, hdrs)

    def export(self) -> bytes:
        resp, data = self.call("GET", "/api/backup")
        self.assertEqual(resp.status, 200)
        return data

    def import_(self, data: bytes, query="", **kw):
        return self.call("POST", "/api/backup/import" + query, data, **kw)

    def refused(self, data: bytes, status: int, text: str, **kw):
        before = everything(self.env)
        resp, body = self.import_(data, **kw)
        self.assertEqual(resp.status, status, body)
        self.assertIn(text, json.loads(body)["error"])
        self.assertEqual(everything(self.env), before)


class ExportTests(BackupCase):
    def test_export_headers_and_content(self):
        resp, data = self.call("GET", "/api/backup")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Length"), str(len(data)))
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        disposition = resp.getheader("Content-Disposition")
        self.assertRegex(
            disposition, r'^attachment; filename="aiolineup-backup-\d{4}-\d{2}-\d{2}\.json"$'
        )
        doc = json.loads(data)
        self.assertEqual(doc["format"], "aiolineup-backup")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["app_version"], __version__)
        self.assertRegex(doc["exported_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(
            sorted(doc),
            sorted(
                [
                    "format",
                    "version",
                    "exported_at",
                    "app_version",
                    "sources",
                    "selections",
                    "categories",
                    "channel_edits",
                    "stream_prefs",
                    "epg_mappings",
                    "settings",
                ]
            ),
        )
        # Source URLs are exported exactly, logins included.
        self.assertIn("https://a/a.m3u?user=me&pass=secret", [s["url"] for s in doc["sources"]])
        self.assertEqual(len(doc["selections"]), 3)
        self.assertEqual({s["number"] for s in doc["selections"]}, {1, 2, 40})

    def test_export_holds_no_secret_cache_or_stats(self):
        text = self.export().decode()
        self.assertNotIn(self.env.cfg.feed_token, text)
        self.assertNotIn(self.env.cfg.admin_password, text)
        # Cached playlist rows stay out; only the saved stream choices name their streams.
        self.assertNotIn("speed1.m3u8", text)
        self.assertNotIn("topgear.m3u8", text)
        self.assertNotIn("playlist_stats", text)
        self.assertNotIn("epg_coverage", text)
        session = self.env.dir / "session_secret"
        if session.exists():
            self.assertNotIn(session.read_text().strip(), text)

    def test_export_needs_login(self):
        resp, _ = self.call("GET", "/api/backup", cookie=False)
        self.assertEqual(resp.status, 401)

    def test_export_writes_nothing(self):
        before = everything(self.env)
        self.export()
        self.assertEqual(everything(self.env), before)


class RoundTripTests(BackupCase):
    def fresh(self):
        env = TempEnv(PORT="0", HOST="127.0.0.1", PUBLIC_URL="https://lineup.example")
        self.addCleanup(env.close)
        port, cookie = self.start(env)
        return env, port, cookie

    def test_round_trip_gives_the_same_feeds(self):
        data = self.export()
        old = self.env
        new, port, cookie = self.fresh()
        resp, body = self.raw(
            port, "POST", "/api/backup/import", data, {**HEADERS, "Cookie": cookie}
        )
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(json.loads(body)["refreshing"], 3)
        self.assertEqual(user_data(new), user_data(old))
        # Refresh from the same upstream fixtures: the worker would run exactly this.
        new.engine._execute(set(new.engine.pending()["refresh"]), True, False)
        new.engine.build_epg()
        self.assertEqual(
            new.engine.playlist_path.read_bytes(), old.engine.playlist_path.read_bytes()
        )
        self.assertEqual(new.engine.epg_path.read_bytes(), old.engine.epg_path.read_bytes())
        self.assertEqual(user_data(new), user_data(old))

    def test_import_replaces_everything_and_queues_a_full_refresh(self):
        data = self.export()
        env, port, cookie = self.fresh()
        with env.db.conn() as c:
            c.execute(
                "INSERT INTO sources (id, kind, name, url, created_at)"
                " VALUES (3, 'm3u', 'Old', 'https://old/o.m3u', 0)"
            )
            c.execute(
                "INSERT INTO streams (source_id, pos, channel_key, title, url, duration, attrs,"
                " directives) VALUES (3, 0, 'id:old', 'Old', 'https://x/o', '-1', '{}', '[]')"
            )
            c.execute("INSERT INTO selections VALUES ('id:old', 'Old', 1, 1)")
        env.db.set_kv("playlist_stats", {"x": 1})
        env.db.set_kv("epg_coverage", {"id:old": {"source_id": 3}})
        env.engine.request_refresh([3])
        stale = [env.engine.raw_path(3), env.engine.fragment_path(3), env.engine.raw_path(9)]
        for p in stale:
            p.write_bytes(b"old")
        resp, body = self.raw(
            port, "POST", "/api/backup/import", data, {**HEADERS, "Cookie": cookie}
        )
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(
            [r["name"] for r in env.db.query("SELECT name FROM sources ORDER BY id")],
            ["A", "B", "G3", "Off"],
        )
        self.assertIsNone(env.db.one("SELECT 1 FROM streams"))
        self.assertIsNone(env.db.one("SELECT 1 FROM selections WHERE channel_key = 'id:old'"))
        self.assertIsNone(env.db.get_kv("playlist_stats"))
        self.assertIsNone(env.db.get_kv("epg_coverage"))
        self.assertFalse(any(p.exists() for p in stale))
        pending = env.engine.pending()
        self.assertEqual(pending["refresh"], [1, 2, 3])  # the disabled source is not queued
        self.assertTrue(pending["playlist"])
        self.assertIsNotNone(pending["epg_at"])
        # Imported sources start like new ones: nothing fetched yet.
        row = env.db.one("SELECT * FROM sources WHERE id = 1")
        self.assertIsNone(row["last_success"])
        self.assertIsNone(row["etag"])
        self.assertIsNone(row["fragment_key"])

    def test_disabled_language_setting_empties_cached_languages(self):
        data = self.export()
        env, port, cookie = self.fresh()
        with env.db.conn() as c:
            c.execute("INSERT INTO meta_languages VALUES ('x', '[\"en\"]')")
        env.db.set_kv("settings", {"enrich_languages": True})
        resp, _ = self.raw(port, "POST", "/api/backup/import", data, {**HEADERS, "Cookie": cookie})
        self.assertEqual(resp.status, 200)
        self.assertIsNone(env.db.one("SELECT 1 FROM meta_languages"))

    def test_check_returns_a_summary_and_writes_nothing(self):
        data = self.export()
        before = everything(self.env)
        resp, body = self.import_(data, "?check=1")
        self.assertEqual(resp.status, 200, body)
        summary = json.loads(body)
        self.assertEqual(summary["exported_at"], json.loads(data)["exported_at"])
        self.assertEqual(
            summary["counts"], {"sources": 4, "channels": 3, "guide_mappings": 2, "settings": 5}
        )
        self.assertEqual(everything(self.env), before)
        self.assertEqual(self.env.engine.pending()["refresh"], [])


class RefusalTests(BackupCase):
    def mutated(self, change) -> bytes:
        doc = json.loads(self.export())
        change(doc)
        return json.dumps(doc).encode()

    def test_newer_version(self):
        self.refused(self.mutated(lambda d: d.update(version=2)), 400, "made by a newer AIOLineup")

    def test_other_format_version_and_unknown_keys(self):
        self.refused(self.mutated(lambda d: d.update(format="other")), 400, "not an AIOLineup")
        self.refused(self.mutated(lambda d: d.update(version="1")), 400, "version")
        self.refused(self.mutated(lambda d: d.update(extra=1)), 400, "extra")
        self.refused(self.mutated(lambda d: d.pop("settings")), 400, "settings")
        self.refused(self.mutated(lambda d: d["sources"][0].update(token="x")), 400, "sources[0]")

    def test_not_json_or_not_an_object(self):
        self.refused(b"{nope", 400, "Invalid JSON")
        self.refused(b"[1]", 400, "Expected a JSON object")

    def test_bad_rows_name_the_row(self):
        def number(d):
            d["selections"][1]["number"] = 0

        self.refused(self.mutated(number), 400, "selections[1].number")

        def duplicate(d):
            d["selections"][1]["number"] = d["selections"][0]["number"]

        self.refused(self.mutated(duplicate), 400, "selections[1].number")

        def url(d):
            d["sources"][2]["url"] = "ftp://x/y"

        self.refused(self.mutated(url), 400, "sources[2].url")

        def kind(d):
            d["sources"][0]["kind"] = "json"

        self.refused(self.mutated(kind), 400, "sources[0].kind")

        def mapping(d):
            d["epg_mappings"][0]["source_id"] = 99

        self.refused(self.mutated(mapping), 400, "epg_mappings[0].source_id")

        def mapping_to_playlist(d):
            d["epg_mappings"][0]["source_id"] = 1  # a playlist is no guide

        self.refused(self.mutated(mapping_to_playlist), 400, "epg_mappings[0].source_id")

        def categories(d):
            d["categories"][0]["category"] = "a;b"

        self.refused(self.mutated(categories), 400, "categories[0].category")

        def edit(d):
            d["channel_edits"][0]["logo"] = 'https://x/"y'

        self.refused(self.mutated(edit), 400, "channel_edits[0].logo")

        def setting(d):
            d["settings"]["nope"] = 1

        self.refused(self.mutated(setting), 400, "settings")

        def source_id(d):
            d["sources"][1]["id"] = d["sources"][0]["id"]

        self.refused(self.mutated(source_id), 400, "sources[1].id")

        def prefs(d):
            d["stream_prefs"][0]["channel_key"] = "id:not.selected"

        self.refused(self.mutated(prefs), 400, "stream_prefs[0].channel_key")

        def type_(d):
            d["sources"][0]["enabled"] = "yes"

        self.refused(self.mutated(type_), 400, "sources[0].enabled")

    def test_long_text_is_accepted_but_numbers_must_fit_the_database(self):
        def long_name(d):
            d["selections"][0]["name"] = "x" * 5000  # a playlist title has no length limit

        resp, body = self.import_(self.mutated(long_name), "?check=1")
        self.assertEqual(resp.status, 200, body)

        def huge(d):
            d["selections"][0]["number"] = 2**63

        self.refused(self.mutated(huge), 400, "selections[0].number")

        def huge_id(d):
            d["sources"][0]["id"] = 2**63

        self.refused(self.mutated(huge_id), 400, "sources[0].id")

    def test_categories_repeating_ignoring_case_are_refused(self):
        def repeat(d):
            d["categories"].append(
                {"channel_key": "id:topgear.uk@sd", "position": 3, "category": "cars"}
            )

        self.refused(self.mutated(repeat), 400, "categories[2].category")

    def test_oversize(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/backup/import")
        for k, v in {
            **HEADERS,
            "Cookie": self.cookie,
            "Content-Type": "application/json",
            "Content-Length": str(backup.MAX_BYTES + 1),
        }.items():
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        conn.close()

    def test_needs_header_and_login(self):
        data = self.export()
        before = everything(self.env)
        resp, _ = self.import_(data, headers={})
        self.assertEqual(resp.status, 403)
        resp, _ = self.import_(data, cookie=False)
        self.assertEqual(resp.status, 401)
        resp, _ = self.import_(data, "?check=1", headers={})
        self.assertEqual(resp.status, 403)
        self.assertEqual(everything(self.env), before)

    def test_busy_worker(self):
        data = self.export()
        self.assertTrue(self.env.engine.job_lock.acquire(blocking=False))
        try:
            self.refused(data, 409, "A refresh is running; try again when it finishes.")
        finally:
            self.env.engine.job_lock.release()
        resp, _ = self.import_(data)
        self.assertEqual(resp.status, 200)

    def test_failed_apply_changes_nothing(self):
        data = json.loads(self.export())
        parsed = backup.check(data)
        before = everything(self.env)
        # A row that passes the checks but breaks the insert: the whole file must roll back.
        parsed.selections.append(parsed.selections[0])
        with self.assertRaises(sqlite3.IntegrityError):
            backup.apply(self.env.engine, parsed)
        self.assertEqual(everything(self.env), before)
        self.assertEqual(self.env.engine.pending()["refresh"], [])


class ApplyEdgeTests(BackupCase):
    def test_a_cache_file_that_cannot_be_removed_does_not_stop_the_import(self):
        data = self.export()
        stale = self.env.engine.raw_path(3)
        stale.write_bytes(b"old")
        real = Path.unlink

        def failing(path, *args, **kwargs):
            if path == stale:
                raise PermissionError("denied")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", failing):
            resp, body = self.import_(data)
        self.assertEqual(resp.status, 200, body)
        pending = self.env.engine.pending()
        self.assertEqual(pending["refresh"], [1, 2, 3])
        self.assertTrue(pending["playlist"])

    def test_export_over_the_import_cap_is_refused_not_downloaded(self):
        with mock.patch.object(backup, "MAX_BYTES", 100):
            resp, body = self.call("GET", "/api/backup")
        self.assertEqual(resp.status, 413)
        self.assertIn("so it was not downloaded", json.loads(body)["error"])


class WorkerLockTests(unittest.TestCase):
    def test_worker_holds_the_job_lock_while_it_runs_a_job(self):
        env = TempEnv()
        self.addCleanup(env.close)
        engine = env.engine
        seen = []
        done = threading.Event()

        def execute(*args):
            seen.append(engine.job_lock.locked())
            done.set()

        engine._execute = execute
        engine.start()
        self.addCleanup(engine.stop)
        done.clear()
        engine.request_playlist()
        self.assertTrue(done.wait(10))
        self.assertEqual(seen[-1:], [True])
        deadline = time.monotonic() + 5
        while engine.job_lock.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(engine.job_lock.locked())


if __name__ == "__main__":
    unittest.main()
