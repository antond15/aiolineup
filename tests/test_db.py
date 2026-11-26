import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aiolineup import db as db_mod
from aiolineup.db import SCHEMA_VERSION, Database, _Conn

V5_SCHEMA = (Path(__file__).parent / "fixtures" / "schema_v5.sql").read_text()
V6_SCHEMA = (Path(__file__).parent / "fixtures" / "schema_v6.sql").read_text()


class _FailingConn:
    """Stands in for sqlite3.Connection; fails on the chosen statement."""

    def __init__(self, fail_on: str):
        self.fail_on = fail_on
        self.in_transaction = False
        self.statements = []

    def execute(self, sql, *args):
        self.statements.append(sql)
        if sql == self.fail_on:
            raise sqlite3.OperationalError("database is locked")
        self.in_transaction = sql == "BEGIN IMMEDIATE"


class ConnTests(unittest.TestCase):
    def _lock_is_free(self, lock) -> bool:
        result = []

        def probe():  # another thread: an RLock held here would still let us in
            got = lock.acquire(timeout=1)
            if got:
                lock.release()
            result.append(got)

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        return result[0]

    def test_failed_begin_releases_write_lock(self):
        lock = threading.RLock()
        conn = _Conn(_FailingConn("BEGIN IMMEDIATE"), lock)
        with self.assertRaises(sqlite3.OperationalError), conn:
            pass
        self.assertTrue(self._lock_is_free(lock))
        self.assertEqual(conn._depth, 0)

    def test_failed_commit_rolls_back_and_releases(self):
        lock = threading.RLock()
        raw = _FailingConn("COMMIT")
        conn = _Conn(raw, lock)
        with self.assertRaises(sqlite3.OperationalError), conn:
            pass
        self.assertEqual(raw.statements, ["BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"])
        self.assertTrue(self._lock_is_free(lock))


class FreshDatabaseTests(unittest.TestCase):
    def test_new_database_starts_without_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "aiolineup.db")
            try:
                self.assertEqual(db.one("SELECT count(*) FROM sources")[0], 0)
                self.assertEqual(db.one("PRAGMA user_version")[0], SCHEMA_VERSION)
            finally:
                db.close()


def _raw(path: Path) -> sqlite3.Connection:
    """A plain connection that reads a file without running Database's upgrade."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _old_file(tmp: str, version: int) -> Path:
    """A database as a real install at `version` (4 or 5) has it, with one row or more in
    every table: the frozen v5 schema, minus category_overrides for v4."""
    path = Path(tmp) / "aiolineup.db"
    conn = _raw(path)
    conn.executescript(V5_SCHEMA)
    attrs = json.dumps
    with conn:
        conn.executemany(
            "INSERT INTO sources (id, kind, name, url, enabled, priority, created_at, etag,"
            " item_count, fragment_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "m3u", "iptv-org", "https://a/a.m3u", 1, 10, 100.0, '"e1"', 6, None),
                (2, "m3u", "Backup", "https://b/b.m3u", 1, 20, 101.0, None, 2, None),
                (3, "m3u", "Old", "https://c/c.m3u", 0, 5, 102.0, None, 1, None),
                (9, "xmltv", "Guide", "https://g/g.xml", 1, 10, 103.0, None, 2, "abc"),
            ],
        )
        streams = [
            # (source, pos, key, title, attrs)
            (1, 0, "id:redbulltv.at@sd", "Red Bull TV (1080p)",
             {"tvg-id": "RedBullTV.at@SD", "tvg-logo": "https://l/rb.png", "group-title": "Sports"}),
            (1, 1, "id:ct1.cz", "ČT1", {"tvg-id": "CT1.cz"}),
            (1, 2, "name:arte", "arte", {}),
            (1, 3, "id:zulu.za", "", {"tvg-id": "Zulu.za"}),  # no title: the id is the name
            (1, 4, "id:arte.fr", "ARTE [Geo-blocked]", {"tvg-id": "Arte.fr"}),
            (1, 5, "name:unselected", "Unselected", {}),
            (2, 0, "id:zdf.de", "ZDF", {"tvg-id": "ZDF.de"}),
            (2, 1, "id:ct1.cz", "Česká jednička", {"tvg-id": "CT1.cz"}),  # lower priority
            (3, 0, "id:only.disabled", "Aaa Disabled", {"tvg-id": "Only.disabled"}),
        ]  # fmt: skip
        conn.executemany(
            "INSERT INTO streams VALUES (?, ?, ?, ?, ?, '-1', ?, ?)",
            [
                (sid, pos, key, title, f"https://s/{sid}/{pos}", attrs(a), attrs([]))
                for sid, pos, key, title, a in streams
            ],
        )
        # Stored names are what each channel was called when selected; the feed sorts by
        # today's catalogue name, so several differ on purpose. added_at is not name order.
        conn.executemany(
            "INSERT INTO selections VALUES (?, ?, ?)",
            [
                ("id:redbulltv.at@sd", "Zz Old Red Bull", 1.0),
                ("id:zulu.za", "Zulu", 2.0),
                ("id:gone", "Beta Gone", 3.0),  # no streams anywhere: missing
                ("id:only.disabled", "Mmm Missing Disabled", 4.0),  # only a disabled source
                ("id:ct1.cz", "ČT1", 5.0),
                ("name:arte", "arte", 6.0),
                ("id:zdf.de", "ZDF", 7.0),
                ("id:arte.fr", "ARTE", 8.0),
            ],
        )
        conn.execute("INSERT INTO epg_mappings VALUES ('id:ct1.cz', 'CT1.cz', 9)")
        conn.execute("INSERT INTO epg_mappings VALUES ('id:gone', '', NULL)")
        conn.execute("INSERT INTO category_overrides VALUES ('id:zdf.de', 'News')")
        conn.execute("INSERT INTO epg_channels VALUES (9, 'CT1.cz', 'ct1.cz', 'ČT1', 4, 1, 2)")
        conn.execute("INSERT INTO epg_schedule VALUES (9, 'ct1.cz', 1, 2, 'Zprávy')")
        conn.execute("INSERT INTO meta_languages VALUES ('ct1.cz', '[\"ces\"]')")
        conn.execute("INSERT INTO kv VALUES ('settings', '{\"epg_days\": 3}')")
        conn.execute("INSERT INTO kv VALUES ('playlist_stats', '{\"channels\": 6}')")
        if version == 4:
            conn.execute("DROP TABLE category_overrides")
        conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    return path


def _snapshot(conn, skip: tuple[str, ...]) -> dict[str, list[str]]:
    """Every row of every table not in `skip`, order-independent."""
    names = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if r[0] not in skip and not r[0].startswith("sqlite_")
    ]
    return {n: sorted(repr(tuple(r)) for r in conn.execute(f"SELECT * FROM {n}")) for n in names}


def _shape(conn) -> dict:
    """Tables, indexes and their columns: what must match between a migrated and a new file."""
    out = {}
    for kind, name in conn.execute("SELECT type, name FROM sqlite_master ORDER BY type, name"):
        pragma = "table_xinfo" if kind == "table" else "index_xinfo"
        out[(kind, name)] = [tuple(r) for r in conn.execute(f"PRAGMA {pragma}({name})")]
        if kind == "table":
            out[("indexes", name)] = sorted(
                tuple(r)[1:] for r in conn.execute(f"PRAGMA index_list({name})")
            )
    return out


NEW_TABLES = ("stream_prefs", "channel_edits")


class MigrationTests(unittest.TestCase):
    """Upgrades are safe because SCHEMA (CREATE TABLE IF NOT EXISTS) creates new tables before
    _migrate runs, and each step rewrites only what it names."""

    def test_v5_selections_are_numbered_alphabetically_as_the_feed_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _old_file(tmp, 5)
            raw = _raw(path)
            before = _snapshot(raw, skip=("selections",))
            selections_before = sorted(raw.execute("SELECT * FROM selections").fetchall())
            raw.close()
            db = Database(path)
            try:
                self.assertEqual(db.one("PRAGMA user_version")[0], SCHEMA_VERSION)
                self.assertEqual(db.upgraded_from, 5)
                rows = db.query("SELECT channel_key, number FROM selections ORDER BY number")
                # Feed order of schema 5: (fold(catalogue name), key); a missing channel has
                # only its stored name. "ARTE [Geo-blocked]" cleans to "ARTE" and ties "arte".
                self.assertEqual(
                    [(r[0], r[1]) for r in rows],
                    [
                        ("id:arte.fr", 1),  # "arte"
                        ("name:arte", 2),  # "arte", later key
                        ("id:gone", 3),  # "beta gone" (stored)
                        ("id:ct1.cz", 4),  # "ct1", the priority-10 title
                        ("id:only.disabled", 5),  # "mmm missing disabled" (stored)
                        ("id:redbulltv.at@sd", 6),  # "red bull tv", not the stored "zz old"
                        ("id:zdf.de", 7),
                        ("id:zulu.za", 8),  # "zulu.za": no title, so the id
                    ],
                )
                # Name and added_at of every selection are kept as they were.
                self.assertEqual(
                    sorted(
                        tuple(r)
                        for r in db.query("SELECT channel_key, name, added_at FROM selections")
                    ),
                    selections_before,
                )
                # Every other table is byte-for-byte equal; the new ones are empty, and the
                # saved category became a one-item list (the v7 step).
                after = _snapshot(db.conn(), skip=("selections", "category_overrides", *NEW_TABLES))
                self.assertEqual(
                    after, {k: v for k, v in before.items() if k != "category_overrides"}
                )
                self.assertEqual(
                    [tuple(r) for r in db.query("SELECT * FROM category_overrides")],
                    [("id:zdf.de", 1, "News")],
                )
                for table in NEW_TABLES:
                    self.assertEqual(db.one(f"SELECT count(*) FROM {table}")[0], 0)
                self.assertEqual(db.one("PRAGMA integrity_check")[0], "ok")
                self.assertEqual(db.query("PRAGMA foreign_key_check"), [])
            finally:
                db.close()

    def test_migrated_file_has_the_shape_of_a_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            Database(Path(tmp) / "new.db").close()
            fresh = _raw(Path(tmp) / "new.db")
            Database(_old_file(tmp, 5)).close()
            migrated = _raw(Path(tmp) / "aiolineup.db")
            try:
                self.assertEqual(_shape(migrated), _shape(fresh))
            finally:
                fresh.close()
                migrated.close()

    def test_numbers_are_unique_positive_and_required_after_the_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(_old_file(tmp, 5))
            try:
                for number in (1, 0, None):  # taken, below 1, absent
                    with self.assertRaises(sqlite3.IntegrityError), db.conn() as c:
                        c.execute(
                            "INSERT INTO selections VALUES ('id:new', 'New', 9, ?)", (number,)
                        )
            finally:
                db.close()

    def test_v5_without_selections_upgrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _old_file(tmp, 5)
            raw = _raw(path)
            with raw:
                raw.execute("DELETE FROM selections")
            raw.close()
            db = Database(path)
            try:
                self.assertEqual(db.one("PRAGMA user_version")[0], SCHEMA_VERSION)
                self.assertEqual(db.one("SELECT count(*) FROM selections")[0], 0)
            finally:
                db.close()

    def test_v4_database_gains_an_empty_category_table_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _old_file(tmp, 4)
            raw = _raw(path)
            before = _snapshot(raw, skip=("selections",))
            raw.close()
            db = Database(path)
            try:
                self.assertEqual(db.one("PRAGMA user_version")[0], SCHEMA_VERSION)
                self.assertEqual(db.upgraded_from, 4)
                self.assertEqual(db.one("SELECT count(*) FROM category_overrides")[0], 0)
                skip = ("selections", "category_overrides", *NEW_TABLES)
                self.assertEqual(_snapshot(db.conn(), skip), before)
                self.assertEqual(before["sources"][3].count("'abc'"), 1)  # v4 step not re-run
                self.assertEqual(db.one("SELECT count(*) FROM selections")[0], 8)
            finally:
                db.close()

    def test_schema_creates_the_tables_before_migrate_runs(self):
        seen = []
        real = Database._migrate

        def spy(c, version):
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
            seen.append((version, {"category_overrides", *NEW_TABLES} <= tables))
            real(c, version)

        with tempfile.TemporaryDirectory() as tmp:
            path = _old_file(tmp, 4)
            with mock.patch.object(Database, "_migrate", staticmethod(spy)):
                db = Database(path)
            db.close()
        self.assertEqual(seen, [(4, True)])

    def test_failed_upgrade_leaves_the_v5_file_as_it_was(self):
        """The fact the v6 upgrade is safe because of: the whole step, DROP TABLE and RENAME
        included, is one SQLite transaction, so a failure at any point (here before the first
        statement, and after the last one) leaves every v5 row and the v5 schema."""
        real = db_mod._number_selections

        def after_rebuild(c):
            real(c)  # selections dropped, rebuilt and renamed; nothing committed yet
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
            assert "selections_v6" not in names and "selections" in names
            raise RuntimeError("boom")

        failures = {
            "before": mock.patch("aiolineup.m3u.fold", side_effect=RuntimeError("boom")),
            "after": mock.patch.object(db_mod, "_number_selections", after_rebuild),
        }
        for when, patch in failures.items():
            with self.subTest(when), tempfile.TemporaryDirectory() as tmp:
                path = _old_file(tmp, 5)
                raw = _raw(path)
                # The new tables are created empty by SCHEMA before the upgrade transaction.
                before = _snapshot(raw, skip=NEW_TABLES)
                schema = raw.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'selections'"
                ).fetchone()
                raw.close()
                with patch, self.assertRaises(RuntimeError):
                    Database(path)
                raw = _raw(path)
                try:
                    self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 5)
                    self.assertEqual(_snapshot(raw, skip=NEW_TABLES), before)
                    self.assertEqual(
                        raw.execute(
                            "SELECT sql FROM sqlite_master WHERE name = 'selections'"
                        ).fetchone(),
                        schema,
                    )
                    self.assertEqual(
                        [tuple(r) for r in raw.execute("SELECT count(*) FROM stream_prefs")],
                        [(0,)],
                    )
                finally:
                    raw.close()
                db = Database(path)  # and the next start upgrades it
                try:
                    self.assertEqual(
                        db.one("SELECT count(*) FROM selections WHERE number > 0")[0], 8
                    )
                finally:
                    db.close()

    def test_v7_step_is_skipped_when_schema_made_the_list_table(self):
        """A v4 file has no category table, so SCHEMA creates the v7 one before _migrate."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(_old_file(tmp, 4))
            try:
                cols = [r[1] for r in db.query("PRAGMA table_info(category_overrides)")]
                self.assertEqual(cols, ["channel_key", "position", "category"])
            finally:
                db.close()

    def test_current_database_is_not_marked_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aiolineup.db"
            Database(path).close()
            db = Database(path)
            try:
                self.assertIsNone(db.upgraded_from)
            finally:
                db.close()


def _v6_file(tmp: str) -> Path:
    """A database as a real v6 install has it, with one row or more in every table and saved
    categories that must survive byte for byte (accents, case, a channel no longer selected
    and one missing upstream)."""
    path = Path(tmp) / "aiolineup.db"
    conn = _raw(path)
    conn.executescript(V6_SCHEMA)
    attrs = json.dumps
    with conn:
        conn.executemany(
            "INSERT INTO sources (id, kind, name, url, enabled, priority, created_at, etag,"
            " item_count, fragment_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "m3u", "iptv-org", "https://a/a.m3u", 1, 10, 100.0, '"e1"', 3, None),
                (9, "xmltv", "Guide", "https://g/g.xml", 1, 10, 103.0, None, 2, "abc"),
            ],
        )
        conn.executemany(
            "INSERT INTO streams VALUES (?, ?, ?, ?, ?, '-1', ?, ?)",
            [
                (1, 0, "id:ct1.cz", "ČT1", "https://s/0", attrs({"tvg-id": "CT1.cz", "group-title": "General"}), attrs([])),
                (1, 1, "id:zdf.de", "ZDF", "https://s/1", attrs({"tvg-id": "ZDF.de"}), attrs(["#EXTVLCOPT:x=1"])),
                (1, 2, "name:arte", "arte", "https://s/2", attrs({}), attrs([])),
            ],
        )  # fmt: skip
        conn.executemany(
            "INSERT INTO selections VALUES (?, ?, ?, ?)",
            [
                ("id:ct1.cz", "ČT1", 1.0, 3),
                ("id:zdf.de", "ZDF", 2.0, 1),
                ("id:gone", "Gone", 3.0, 7),
            ],
        )
        conn.execute("INSERT INTO stream_prefs VALUES ('id:zdf.de', 'sig', 1, 0)")
        conn.execute("INSERT INTO channel_edits VALUES ('id:ct1.cz', 'Jednička', NULL)")
        conn.execute("INSERT INTO epg_mappings VALUES ('id:ct1.cz', 'CT1.cz', 9)")
        conn.executemany(
            "INSERT INTO category_overrides VALUES (?, ?)",
            [
                ("id:zdf.de", "News"),
                ("id:ct1.cz", "Zprávy a sport"),  # accents and spaces kept
                ("name:arte", "kultur"),  # not selected: kept, like other edits
                ("id:gone", "Sci-Fi"),  # missing upstream
            ],
        )
        conn.execute("INSERT INTO epg_channels VALUES (9, 'CT1.cz', 'ct1.cz', 'ČT1', 4, 1, 2)")
        conn.execute("INSERT INTO epg_schedule VALUES (9, 'ct1.cz', 1, 2, 'Zprávy')")
        conn.execute("INSERT INTO meta_languages VALUES ('ct1.cz', '[\"ces\"]')")
        conn.execute("INSERT INTO kv VALUES ('settings', '{\"epg_days\": 3}')")
        conn.execute("PRAGMA user_version = 6")
    conn.close()
    return path


class V6MigrationTests(unittest.TestCase):
    """v7 rebuilds category_overrides as an ordered list. Safe because the rebuild runs inside
    the single upgrade transaction and maps every v6 row to exactly one row with the same key
    and text at position 1, so the feed of every edited channel is unchanged."""

    def test_saved_categories_become_one_item_lists_and_nothing_else_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _v6_file(tmp)
            raw = _raw(path)
            before = _snapshot(raw, skip=("category_overrides",))
            saved = sorted(tuple(r) for r in raw.execute("SELECT * FROM category_overrides"))
            raw.close()
            db = Database(path)
            try:
                self.assertEqual(db.one("PRAGMA user_version")[0], SCHEMA_VERSION)
                self.assertEqual(db.upgraded_from, 6)
                self.assertEqual(
                    sorted(tuple(r) for r in db.query("SELECT * FROM category_overrides")),
                    [(key, 1, cat) for key, cat in saved],
                )
                self.assertEqual(_snapshot(db.conn(), skip=("category_overrides",)), before)
                self.assertEqual(db.one("PRAGMA integrity_check")[0], "ok")
                self.assertEqual(db.query("PRAGMA foreign_key_check"), [])
            finally:
                db.close()

    def test_migrated_file_has_the_shape_of_a_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            Database(Path(tmp) / "new.db").close()
            fresh = _raw(Path(tmp) / "new.db")
            Database(_v6_file(tmp)).close()
            migrated = _raw(Path(tmp) / "aiolineup.db")
            try:
                self.assertEqual(_shape(migrated), _shape(fresh))
            finally:
                fresh.close()
                migrated.close()

    def test_positions_are_unique_per_channel_and_start_at_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(_v6_file(tmp))
            try:
                with db.conn() as c:  # a second item for the same channel is fine
                    c.execute("INSERT INTO category_overrides VALUES ('id:zdf.de', 2, 'Kids')")
                for row in (("id:zdf.de", 1, "Again"), ("id:x", 0, "Zero"), ("id:x", None, "N")):
                    with self.assertRaises(sqlite3.IntegrityError), db.conn() as c:
                        c.execute("INSERT INTO category_overrides VALUES (?, ?, ?)", row)
            finally:
                db.close()

    def test_failed_upgrade_leaves_the_v6_file_as_it_was(self):
        """The whole v7 step, DROP TABLE and RENAME included, is one SQLite transaction."""
        real = db_mod._list_categories

        def after_rebuild(c):
            real(c)
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
            assert "category_overrides_v7" not in names and "category_overrides" in names
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            path = _v6_file(tmp)
            raw = _raw(path)
            before = _snapshot(raw, skip=())
            schema = raw.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'category_overrides'"
            ).fetchone()
            raw.close()
            with (
                mock.patch.object(db_mod, "_list_categories", after_rebuild),
                self.assertRaises(RuntimeError),
            ):
                Database(path)
            raw = _raw(path)
            try:
                self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(_snapshot(raw, skip=()), before)
                self.assertEqual(
                    raw.execute(
                        "SELECT sql FROM sqlite_master WHERE name = 'category_overrides'"
                    ).fetchone(),
                    schema,
                )
            finally:
                raw.close()
            db = Database(path)  # and the next start upgrades it
            try:
                self.assertEqual(db.one("SELECT count(*) FROM category_overrides")[0], 4)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
