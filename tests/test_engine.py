import io
import json
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from aiolineup import engine as engine_mod
from aiolineup import fetch, m3u
from aiolineup.db import SCHEMA_VERSION
from aiolineup.engine import parse_feed_languages
from tests.helpers import PLAYLIST_A, PLAYLIST_B, TempEnv, guide, write_gz


def xt(offset_hours: float) -> str:
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(time.time() + offset_hours * 3600))


class FakeUpstream:
    """Patch fetch.download_to_file with an in-memory URL table."""

    def __init__(self):
        self.content: dict[str, bytes | Exception] = {}
        self.calls: list[str] = []

    def _get(self, url, etag=None):
        self.calls.append(url)
        value = self.content.get(url, fetch.FetchError("HTTP 404"))
        if isinstance(value, Exception):
            raise value
        if etag and etag == f'"{hash(value)}"':
            raise fetch.NotModified()
        return value

    def download_to_file(self, url, dest, **kw):
        data = self._get(url, kw.get("etag"))
        Path(dest).write_bytes(data)
        return fetch.FetchResult(url, f'"{hash(data)}"', None, len(data))

    def __enter__(self):
        self._patches = [
            mock.patch.object(fetch, "download_to_file", self.download_to_file),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.engine = self.env.engine
        self.db = self.env.db
        with self.db.conn() as c:
            c.execute("DELETE FROM sources")
            now = time.time()
            c.execute(
                "INSERT INTO sources (id, kind, name, url, priority, created_at) VALUES (1, 'm3u', 'A', 'https://a/a.m3u', 10, ?)",
                (now,),
            )
            c.execute(
                "INSERT INTO sources (id, kind, name, url, priority, created_at) VALUES (2, 'm3u', 'B', 'https://b/b.m3u', 20, ?)",
                (now,),
            )
        self.up = FakeUpstream().__enter__()
        self.up.content["https://a/a.m3u"] = PLAYLIST_A.encode()
        self.up.content["https://b/b.m3u"] = PLAYLIST_B.encode()
        self.db.set_kv("settings", {"enrich_languages": False})

    def tearDown(self):
        self.up.__exit__()
        self.env.close()

    def refresh(self, *ids):
        self.engine._execute(set(ids or (1, 2)), True, False)

    def playlist(self) -> list[m3u.Entry]:
        return m3u.parse_text(self.engine.playlist_path.read_text()).entries

    # ------------------------------------------------------------ catalogue / playlist

    def test_aggregation_grouping_and_dedup(self):
        self.refresh()
        cat = self.engine.catalog()
        self.assertEqual(
            set(cat),
            {"id:redbulltv.at@sd", "id:topgear.uk@sd", "name:speed sport 1", "id:nbatv.us@sd"},
        )
        rb = cat["id:redbulltv.at@sd"]
        self.assertEqual(rb.name, "Red Bull TV")
        self.assertEqual(
            rb.streams, 3
        )  # 1080, 720 (with UA), backup; the B copy of 1080 is a duplicate
        self.assertEqual(rb.categories, ["Sports", "Outdoor"])
        self.assertEqual(rb.countries, ["AT"])
        self.assertEqual(rb.sources, [1, 2])
        self.assertEqual(cat["name:speed sport 1"].streams, 2)

    def test_whitelist_nothing_selected_by_default(self):
        self.refresh()
        self.assertEqual(self.playlist(), [])
        self.assertTrue(self.engine.playlist_path.read_text().startswith("#EXTM3U"))

    def test_playlist_contains_only_selected_with_alternatives(self):
        self.refresh()
        self.engine.set_selection(["id:redbulltv.at@sd", "name:speed sport 1"], [])
        self.engine.build_playlist()
        entries = self.playlist()
        urls = [e.url for e in entries]
        self.assertEqual(
            urls,
            [
                "https://cdn.example.org/redbull/1080.m3u8",
                "https://cdn.example.org/redbull/720.m3u8",
                "https://backup.example.org/redbull.m3u8",
                "https://cdn.example.org/speed1.m3u8",
                "https://mirror.example.org/speed1.m3u8",
            ],
        )
        self.assertEqual(entries[1].directives, ["#EXTVLCOPT:http-user-agent=Mozilla/5.0"])
        self.assertEqual(entries[0].attr("tvg-logo"), "https://example.org/rb.png")
        self.assertEqual(
            entries[2].attr("tvg-logo"), "https://example.org/rb.png"
        )  # filled from channel
        # Channel without upstream tvg-id gets one shared id so players group its alternatives.
        self.assertEqual({entries[3].tvg_id, entries[4].tvg_id}, {"Speed Sport 1"})

    def test_feed_group_title_lists_every_source_category_of_the_channel(self):
        self.refresh()
        self.engine.set_selection(
            ["id:redbulltv.at@sd", "id:topgear.uk@sd", "name:speed sport 1"], []
        )
        self.engine.build_playlist()
        groups = {e.tvg_id: [] for e in self.playlist()}
        for e in self.playlist():
            groups[e.tvg_id].append(e.attr("group-title"))
        # Every category in catalogue order (source priority), the same value on every
        # stream, whichever source the stream came from.
        self.assertEqual(set(groups["RedBullTV.at@SD"]), {"Sports;Outdoor"})
        self.assertEqual(set(groups["TopGear.uk@SD"]), {"Entertainment;Auto"})
        self.assertEqual(set(groups["Speed Sport 1"]), {"Sports"})

    def test_feed_sends_the_first_8_categories_without_case_repeats(self):
        cats = ";".join(["News", "news", *(f"C{i}" for i in range(1, 10))])  # 10 distinct
        self.up.content["https://a/a.m3u"] = (
            f'#EXTM3U\n#EXTINF:-1 tvg-id="n.x" group-title="{cats}",Plain\nhttps://s/1\n'
        ).encode()
        self.refresh(1)
        ch = self.engine.catalog()["id:n.x"]
        self.assertEqual(len(ch.shown_categories), 11)  # the app shows every source category
        self.assertEqual(ch.feed_categories, ["News", *(f"C{i}" for i in range(1, 8))])
        self.engine.set_selection(["id:n.x"], [])
        self.engine.build_playlist()
        (entry,) = self.playlist()
        self.assertEqual(entry.attr("group-title"), "News;C1;C2;C3;C4;C5;C6;C7")

    def test_feed_has_no_group_title_or_extgrp_without_a_category(self):
        self.up.content["https://a/a.m3u"] = (
            b'#EXTM3U\n#EXTINF:-1 tvg-id="n.x" group-title="Undefined",Plain\n'
            b"#EXTGRP:Old\nhttps://s/1\n"
            b'#EXTINF:-1 tvg-id="g.x" GROUP-TITLE="Other",Grp\n#EXTGRP:Movies\nhttps://s/2\n'
        )
        self.refresh(1)
        self.engine.set_selection(["id:n.x", "id:g.x"], [])
        self.engine.build_playlist()
        text = self.engine.playlist_path.read_text()
        self.assertNotIn("EXTGRP", text)
        self.assertNotIn("group-title", text.lower())

    def test_own_categories_replace_source_categories_in_feed_and_catalog(self):
        self.refresh()
        self.engine.set_selection(["id:redbulltv.at@sd", "name:speed sport 1"], [])
        stored = self.engine.set_categories("id:redbulltv.at@sd", ["News", "Kids", "Docs"])
        self.assertEqual(stored, ["News", "Kids", "Docs"])
        ch = self.engine.catalog()["id:redbulltv.at@sd"]
        self.assertEqual(ch.shown_categories, ["News", "Kids", "Docs"])  # the order given
        self.assertEqual(ch.feed_categories, ["News", "Kids", "Docs"])
        self.assertEqual(ch.categories, ["Sports", "Outdoor"])
        self.engine.build_playlist()
        by_id = {}
        for e in self.playlist():
            by_id.setdefault(e.tvg_id, set()).add(e.attr("group-title"))
        self.assertEqual(by_id["RedBullTV.at@SD"], {"News;Kids;Docs"})
        self.assertEqual(by_id["Speed Sport 1"], {"Sports"})
        # Survives a refresh; a shorter list replaces the whole list; a reset returns to the
        # source categories.
        self.refresh()
        self.assertEqual(
            self.engine.catalog()["id:redbulltv.at@sd"].shown_categories, ["News", "Kids", "Docs"]
        )
        self.engine.set_categories("id:redbulltv.at@sd", ["Docs"])
        self.assertEqual(self.engine.catalog()["id:redbulltv.at@sd"].shown_categories, ["Docs"])
        self.assertEqual(
            [tuple(r) for r in self.db.query("SELECT position, category FROM category_overrides")],
            [(1, "Docs")],
        )
        self.assertIsNone(self.engine.set_categories("id:redbulltv.at@sd", None))
        ch = self.engine.catalog()["id:redbulltv.at@sd"]
        self.assertEqual((ch.shown_categories, ch.own_categories), (["Sports", "Outdoor"], []))

    def test_categories_take_the_spelling_of_existing_ones(self):
        self.refresh()
        self.assertEqual(
            self.engine.set_categories("id:topgear.uk@sd", ["sPORTS", "Brand New", "outdoor"]),
            ["Sports", "Brand New", "Outdoor"],
        )
        with self.assertRaises(KeyError):
            self.engine.set_categories("id:nope", ["News"])

    def test_categories_refuse_repeats_ignoring_case_and_bad_lengths(self):
        self.refresh()
        for bad in (["News", "news"], ["Sports", "SPORTS"], [], [f"C{i}" for i in range(9)]):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.engine.set_categories("id:topgear.uk@sd", bad)
        self.assertEqual(self.db.query("SELECT count(*) FROM category_overrides")[0][0], 0)
        eight = [f"C{i}" for i in range(8)]
        self.assertEqual(self.engine.set_categories("id:topgear.uk@sd", eight), eight)

    def test_category_can_be_recased_when_no_other_channel_uses_it(self):
        self.refresh()
        self.assertEqual(self.engine.set_categories("id:topgear.uk@sd", ["Sci-fi"]), ["Sci-fi"])
        self.assertEqual(self.engine.set_categories("id:topgear.uk@sd", ["Sci-Fi"]), ["Sci-Fi"])
        self.assertEqual(self.engine.catalog()["id:topgear.uk@sd"].feed_categories, ["Sci-Fi"])

    def test_category_edit_rebuilds_the_feed_only_for_a_selected_channel(self):
        self.refresh()
        self.engine.set_categories("id:topgear.uk@sd", ["News"])
        self.assertFalse(self.engine.pending()["playlist"])
        self.engine.set_selection(["id:topgear.uk@sd"], [])
        self.engine._pending_playlist = False
        self.engine.set_categories("id:topgear.uk@sd", ["Docs"])
        self.assertTrue(self.engine.pending()["playlist"])

    def test_extgrp_spellings_are_dropped_and_do_not_split_duplicates(self):
        self.up.content["https://a/a.m3u"] = (
            b'#EXTM3U\n#EXTINF:-1 tvg-id="n.x" group-title="News",Plain\n'
            b"#EXTGRP:Old\n#EXTVLCOPT:http-user-agent=x\nhttps://s/1\n"
            b'#EXTINF:-1 tvg-id="n.x",Plain\n#EXTGRP :Other\n#EXTVLCOPT:http-user-agent=x\n'
            b"https://s/1\n"
            b'#EXTINF:-1 tvg-id="n.x",Plain\n#extgrp\n#EXTVLCOPT:http-user-agent=x\nhttps://s/1\n'
        )
        self.refresh(1)
        self.assertEqual(self.engine.catalog()["id:n.x"].streams, 1)
        self.engine.set_selection(["id:n.x"], [])
        self.engine.build_playlist()
        text = self.engine.playlist_path.read_text()
        self.assertNotIn("EXTGRP", text.upper())
        self.assertEqual(text.count("https://s/1"), 1)
        self.assertIn("#EXTVLCOPT:http-user-agent=x", text)  # request directives still pass

    def test_start_queues_both_builds_when_feed_files_exist(self):
        self.engine.playlist_path.write_text("#EXTM3U\nstale\n")
        self.engine.epg_path.write_text("<tv/>")
        self.engine.epg_gz_path.write_bytes(b"")
        for version in (4, None):  # an upgraded and a plain database
            with self.db.conn() as c:
                if version is not None:
                    c.execute(f"PRAGMA user_version = {version}")
            self.engine = self.env.reopen()
            self.db = self.env.db
            with (
                mock.patch.object(self.engine, "build_playlist") as playlist,
                mock.patch.object(self.engine, "build_epg") as epg,
                mock.patch.object(self.engine, "request_playlist") as rp,
                mock.patch.object(self.engine, "request_epg") as re_,
                mock.patch.object(self.engine, "_run"),
            ):
                self.engine.start()
                self.engine.stop()
            playlist.assert_not_called()
            epg.assert_not_called()
            rp.assert_called_once_with()
            re_.assert_called_once_with()

    def test_start_builds_the_guide_when_only_the_gzip_is_missing(self):
        self.engine.playlist_path.write_text("#EXTM3U\n")
        self.engine.epg_path.write_text("<tv/>")
        self.assertFalse(self.engine.epg_gz_path.exists())
        with (
            mock.patch.object(self.engine, "build_epg") as epg,
            mock.patch.object(self.engine, "_run"),
        ):
            self.engine.start()
            self.engine.stop()
        epg.assert_called_once_with()

    def test_deselecting_a_clash_holder_rebuilds_the_guide_at_once(self):
        self.up.content["https://a/a.m3u"] = (
            b'#EXTM3U\n#EXTINF:-1 tvg-id="Nova",Nova\nhttps://s/1\n#EXTINF:-1,Nova\nhttps://s/2\n'
        )
        self.refresh(1)
        self.engine.set_selection(["id:nova"], [])
        with self.db.conn() as c:
            c.execute("UPDATE selections SET added_at = 1")
        self.engine.set_selection(["name:nova"], [])  # no id moves: debounced
        self.assertGreater(self.engine._pending_epg_at - time.time(), 5)
        self.engine.set_selection([], ["id:nova"])  # name:nova goes Nova~2 -> Nova
        self.assertLess(self.engine._pending_epg_at - time.time(), 1)

    def test_ids_differing_only_in_quote_style_do_not_merge_in_the_playlist(self):
        self.up.content["https://a/a.m3u"] = (
            b"#EXTM3U\n#EXTINF:-1,Rock \"Live\"\nhttps://s/1\n#EXTINF:-1,Rock 'Live'\nhttps://s/2\n"
        )
        self.refresh(1)
        self.engine.set_selection(['name:rock "live"', "name:rock 'live'"], [])
        self.engine.build_playlist()
        ids = [e.tvg_id for e in m3u.parse_text(self.engine.playlist_path.read_text()).entries]
        self.assertEqual(len(set(i.lower() for i in ids)), 2, ids)
        self.assertEqual(sorted(self.engine.exported_ids().values()), sorted(ids))

    def test_start_builds_missing_feed_files_synchronously(self):
        with (
            mock.patch.object(self.engine, "build_playlist") as playlist,
            mock.patch.object(self.engine, "build_epg") as epg,
            mock.patch.object(self.engine, "_run"),
        ):
            self.engine.start()
            self.engine.stop()
        playlist.assert_called_once_with()
        epg.assert_called_once_with()

    def test_clashing_selections_get_one_id_in_playlist_and_guide(self):
        self.up.content["https://a/a.m3u"] = (
            b'#EXTM3U\n#EXTINF:-1 tvg-id="Nova",Nova\nhttps://s/1\n'
            b'#EXTINF:-1 tvg-id="NOVA@HD",Nova HD\nhttps://s/2\n'
            b"#EXTINF:-1,Nova\nhttps://s/3\n"
        )
        self.refresh(1)
        keys = ["id:nova", "id:nova@hd", "name:nova"]
        for i, key in enumerate(keys, 1):
            self.engine.set_selection([key], [])
            with self.db.conn() as c:
                c.execute("UPDATE selections SET added_at = ? WHERE channel_key = ?", (i, key))
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"Nova": [(xt(1), xt(2), "Show")]}))
        self.engine._execute({3}, False, False)
        self.engine.build_playlist()
        playlist_ids = [
            e.tvg_id for e in m3u.parse_text(self.engine.playlist_path.read_text()).entries
        ]
        guide_ids, progs = self.epg()
        self.assertEqual(sorted(playlist_ids), ["NOVA@HD~2", "Nova", "Nova~2"])
        self.assertEqual(sorted(guide_ids), sorted(playlist_ids))
        # Every channel, the suffixed name-only one too, still matches the guide by its
        # plain id and is scheduled under its exported id.
        self.assertEqual(sorted(c for c, _ in progs), sorted(playlist_ids))
        self.assertEqual(
            self.engine.exported_ids(),
            {"id:nova": "Nova", "id:nova@hd": "NOVA@HD~2", "name:nova": "Nova~2"},
        )
        self.engine.set_selection([], ["id:nova"])
        self.assertEqual(self.engine.exported_ids()["id:nova@hd"], "NOVA@HD")

    def test_selection_survives_restart_and_refresh(self):
        self.refresh()
        self.engine.set_selection(["id:topgear.uk@sd"], [])
        engine = self.env.reopen()
        self.assertEqual(engine.selected_keys(), {"id:topgear.uk@sd"})
        engine._execute({1, 2}, True, False)
        self.assertEqual(
            [e.tvg_id for e in m3u.parse_text(engine.playlist_path.read_text()).entries],
            ["TopGear.uk@SD"],
        )

    def test_missing_channel_keeps_selection_and_returns(self):
        self.refresh()
        self.engine.set_selection(["id:nbatv.us@sd"], [])
        self.up.content["https://b/b.m3u"] = PLAYLIST_B.replace(
            "NBATV.us@SD", "Other.us@SD"
        ).encode()
        self.refresh()
        self.assertNotIn("id:nbatv.us@sd", self.engine.catalog())
        self.assertIn("id:nbatv.us@sd", self.engine.selected_keys())
        self.assertEqual(self.playlist(), [])
        self.up.content["https://b/b.m3u"] = PLAYLIST_B.encode()
        self.refresh()
        self.assertEqual([e.tvg_id for e in self.playlist()], ["NBATV.us@SD"])

    def test_url_changes_propagate(self):
        self.refresh()
        self.engine.set_selection(["id:topgear.uk@sd"], [])
        self.up.content["https://a/a.m3u"] = PLAYLIST_A.replace(
            "topgear.m3u8", "topgear-v2.m3u8"
        ).encode()
        self.refresh(1)
        self.assertEqual(
            [e.url for e in self.playlist()], ["https://cdn.example.org/topgear-v2.m3u8"]
        )

    def test_cannot_select_unknown_channels(self):
        self.refresh()
        self.assertEqual(self.engine.set_selection(["id:doesnotexist"], []), 0)

    def test_failure_keeps_last_good_data(self):
        self.refresh()
        self.engine.set_selection(["id:topgear.uk@sd"], [])
        self.engine.build_playlist()
        before = self.engine.playlist_path.read_text()
        for bad in (
            fetch.FetchError("Upstream returned HTTP 500."),
            b"<html>Rate limited</html>",
            b"",
        ):
            self.up.content["https://a/a.m3u"] = bad
            self.refresh(1)
            src = self.db.one("SELECT * FROM sources WHERE id = 1")
            self.assertTrue(src["last_error"])
            self.assertEqual(self.engine.playlist_path.read_text(), before)
            if isinstance(bad, bytes) and bad:
                self.assertIn("HTML", src["last_error"])

    def test_not_modified_is_success(self):
        self.refresh(1)
        calls = len(self.up.calls)
        self.refresh(1)
        self.assertEqual(len(self.up.calls), calls + 1)
        src = self.db.one("SELECT * FROM sources WHERE id = 1")
        self.assertIsNone(src["last_error"])
        self.assertEqual(src["item_count"], 4)

    def test_disabled_source_excluded(self):
        self.refresh()
        with self.db.conn() as c:
            c.execute("UPDATE sources SET enabled = 0 WHERE id = 2")
        self.engine.invalidate_catalog()
        self.assertNotIn("id:nbatv.us@sd", self.engine.catalog())
        self.assertEqual(self.engine.catalog()["id:redbulltv.at@sd"].streams, 2)

    # ------------------------------------------------------------ EPG

    def add_guide(self, sid, url, priority, text):
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO sources (id, kind, name, url, priority, created_at) VALUES (?, 'xmltv', ?, ?, ?, ?)",
                (sid, f"G{sid}", url, priority, time.time()),
            )
        path = self.env.dir / f"g{sid}.xml.gz"
        write_gz(path, text)
        self.up.content[url] = path.read_bytes()

    def epg(self):
        import xml.etree.ElementTree as ET

        root = ET.parse(self.engine.epg_path).getroot()
        return (
            [c.get("id") for c in root.findall("channel")],
            [(p.get("channel"), p.findtext("title")) for p in root.findall("programme")],
        )

    def test_epg_matching_priority_mapping_and_failure(self):
        self.refresh()
        self.engine.set_selection(["id:redbulltv.at@sd", "id:topgear.uk@sd", "id:nbatv.us@sd"], [])
        # Guide 3 (preferred) knows Red Bull by its base id; guide 4 has Red Bull too plus NBA.
        self.add_guide(
            3,
            "https://g/3.xml.gz",
            10,
            guide(
                {
                    "RedBullTV.at": [(xt(1), xt(2), "RB from 3")],
                    "Unrelated.us": [(xt(1), xt(2), "Nope")],
                }
            ),
        )
        self.add_guide(
            4,
            "https://g/4.xml.gz",
            20,
            guide(
                {
                    "RedBullTV.at": [(xt(1), xt(2), "RB from 4")],
                    "NBATV.us": [
                        (xt(1), xt(2), "Game"),
                        (xt(-48), xt(-47), "Old game"),
                        (xt(24 * 30), xt(24 * 30 + 1), "Far"),
                    ],
                    "TG.custom": [(xt(3), xt(4), "Top Gear ep")],
                }
            ),
        )
        self.engine._execute({3, 4}, False, False)
        ids, progs = self.epg()
        self.assertEqual(
            sorted(progs),
            sorted(
                [
                    (
                        "RedBullTV.at@SD",
                        "RB from 3",
                    ),  # priority wins, id rewritten to playlist tvg-id
                    ("NBATV.us@SD", "Game"),  # time window drops old + far-future
                ]
            ),
        )
        self.assertIn("TopGear.uk@SD", ids)  # listed without a guide, but no programmes
        self.assertNotIn("TopGear.uk@SD", [c for c, _ in self.epg()[1]])

        # Manual mapping for Top Gear.
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO epg_mappings (channel_key, epg_id) VALUES ('id:topgear.uk@sd', 'TG.custom')"
            )
        self.engine.build_epg()
        self.assertIn(("TopGear.uk@SD", "Top Gear ep"), self.epg()[1])
        cov = self.db.get_kv("epg_coverage")
        self.assertEqual(cov["id:topgear.uk@sd"]["source_id"], 4)

        # Empty mapping disables the guide for a channel.
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO epg_mappings (channel_key, epg_id) VALUES ('id:nbatv.us@sd', '')"
            )
        self.engine.build_epg()
        self.assertNotIn("NBATV.us@SD", [c for c, _ in self.epg()[1]])

        # Upstream breaks: the cached guide stays and keeps producing output.
        before = self.engine.epg_path.read_text()
        self.up.content["https://g/3.xml.gz"] = b"<html>oops</html>"
        self.engine._execute({3}, False, False)
        self.assertIn(
            "XMLTV", self.db.one("SELECT last_error FROM sources WHERE id = 3")["last_error"]
        )
        self.engine.build_epg(force_scan=True)
        self.assertEqual(self.engine.epg_path.read_text(), before)

    def test_stale_higher_priority_source_falls_back_to_next(self):
        # Regression: a preferred source that lists the channel but only has
        # programmes outside the output window used to win, leaving it empty.
        self.refresh()
        self.engine.set_selection(["id:nbatv.us@sd", "id:redbulltv.at@sd"], [])
        self.add_guide(
            3,
            "https://g/3.xml.gz",
            10,
            guide(
                {
                    "NBATV.us": [(xt(-72), xt(-71), "Stale game")],
                    "RedBullTV.at": [(xt(1), xt(2), "RB from 3")],
                }
            ),
        )
        self.add_guide(
            4,
            "https://g/4.xml.gz",
            20,
            guide(
                {
                    "NBATV.us": [(xt(1), xt(2), "Fresh game")],
                    "RedBullTV.at": [(xt(1), xt(2), "RB from 4")],
                }
            ),
        )
        self.engine._execute({3, 4}, False, False)
        self.assertEqual(
            sorted(self.epg()[1]), [("NBATV.us@SD", "Fresh game"), ("RedBullTV.at@SD", "RB from 3")]
        )
        cov = self.db.get_kv("epg_coverage")
        self.assertEqual(cov["id:nbatv.us@sd"]["source_id"], 4)
        self.assertEqual(cov["id:nbatv.us@sd"]["programmes"], 1)

    def test_declared_channel_without_programmes_falls_back(self):
        self.refresh()
        self.engine.set_selection(["id:nbatv.us@sd"], [])
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"NBATV.us": []}))
        self.add_guide(4, "https://g/4.xml.gz", 20, guide({"NBATV.us": [(xt(1), xt(2), "Game")]}))
        self.engine._execute({3, 4}, False, False)
        self.assertEqual(self.epg()[1], [("NBATV.us@SD", "Game")])

    def test_selected_channel_without_guide_is_still_listed(self):
        self.refresh()
        self.engine.set_selection(["id:nbatv.us@sd"], [])
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"Other.us": [(xt(1), xt(2), "Show")]}))
        self.engine._execute({3}, False, False)
        ids, progs = self.epg()
        self.assertEqual(ids, ["NBATV.us@SD"])
        self.assertEqual(progs, [])

    def test_every_selected_channel_has_one_id_in_both_feeds(self):
        # AIOStreams pairs playlist and guide channels by case-insensitive tvg-id, so a
        # channel whose two feeds disagree shows up twice. Covers the id shapes seen live:
        # a case variant across playlists, a guide id without the @feed suffix, the same
        # channel in two guides, a manual mapping, a name-only channel and no guide at all.
        self.refresh()
        self.engine.set_selection(
            ["id:redbulltv.at@sd", "id:topgear.uk@sd", "id:nbatv.us@sd", "name:speed sport 1"],
            [],
        )
        self.add_guide(
            3,
            "https://g/3.xml.gz",
            10,
            guide({"RedBullTV.at": [(xt(1), xt(2), "RB from 3")]}),
        )
        self.add_guide(
            4,
            "https://g/4.xml.gz",
            20,
            guide(
                {
                    "redbulltv.at@sd": [(xt(1), xt(2), "RB from 4")],
                    "TG.custom": [(xt(1), xt(2), "Top Gear ep")],
                }
            ),
        )
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO epg_mappings (channel_key, epg_id) VALUES ('id:topgear.uk@sd', 'TG.custom')"
            )
        self.engine._execute({3, 4}, True, False)

        playlist_ids: dict[str, set[str]] = {}
        for e in self.playlist():
            playlist_ids.setdefault(e.tvg_id.lower(), set()).add(e.title)
        ids, progs = self.epg()
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(ids), len({i.lower() for i in ids}))  # one declaration each
        self.assertEqual({i.lower() for i in ids}, set(playlist_ids))
        self.assertLessEqual({c for c, _ in progs}, set(ids))  # programmes use declared ids
        self.assertEqual(
            sorted(progs),
            [("RedBullTV.at@SD", "RB from 3"), ("TopGear.uk@SD", "Top Gear ep")],
        )

    def test_feeds_read_correctly_by_aiostreams_m3u_reader(self):
        # Mirrors AIOStreams' M3U reader (builtins/m3u at 89f03f6): a channel is the pair
        # tvg-id + title, a guide channel matches by lowercase id, then id without
        # @sd|hd|fhd|uhd|4k, then tvg-name/name against display names of channels that have
        # programmes (names compared lowercase, [\s_]+ as one space).
        import re
        import xml.etree.ElementTree as ET

        self.up.content["https://a/a.m3u"] = (
            b"#EXTM3U\n"
            b'#EXTINF:-1 tvg-id="Nova.cz@SD" tvg-name="Nova Upstream",Nova\nhttps://s/nova1\n'
            b'#EXTINF:-1 tvg-id="Nova.cz@SD" TVG-NAME="Nova Upstream",Nova HD\nhttps://s/nova2\n'
            b'#EXTINF:-1 tvg-id="Nova.sk@SD",NOVA_\nhttps://s/nova3\n'
            b'#EXTINF:-1 tvg-id="Prima.cz@SD",Prima\nhttps://s/prima\n'
        )
        self.refresh(1)
        keys = ["id:nova.cz@sd", "id:nova.sk@sd", "id:prima.cz@sd"]
        self.engine.set_selection(keys, [])
        self.engine.build_playlist()
        self.add_guide(
            3,
            "https://g/3.xml.gz",
            10,
            guide(
                {
                    "Nova.cz": [(xt(1), xt(2), "Nova show")],
                    "Prima.cz@SD": [(xt(1), xt(2), "Prima show")],
                }
            ),
        )
        self.engine._execute({3}, False, False)

        def name_key(s):
            return re.sub(r"[\s_]+", " ", s.lower()).strip()

        channels: dict[tuple[str, str], list[str]] = {}
        for e in self.playlist():
            self.assertEqual(e.attr("tvg-name"), "")
            channels.setdefault((e.tvg_id, e.title), []).append(e.url)
        self.assertEqual(len(channels), 3)  # alternatives with other titles are one channel
        self.assertEqual(len(channels[("Nova.cz@SD", "Nova")]), 2)

        root = ET.parse(self.engine.epg_path).getroot()
        with_programmes = {p.get("channel").lower() for p in root.findall("programme")}
        by_id = {c.get("id").lower(): c for c in root.findall("channel")}
        names: dict[str, str] = {}
        for cid in with_programmes:
            for dn in by_id[cid].findall("display-name"):
                names.setdefault(name_key(dn.text), cid)

        def match(tvg_id, name):
            for k in dict.fromkeys(
                [tvg_id.lower(), re.sub(r"@(sd|hd|fhd|uhd|4k)$", "", tvg_id.lower())]
            ):
                if k in with_programmes:
                    return k
            return names.get(name_key(name))

        self.assertEqual(match("Nova.cz@SD", "Nova"), "nova.cz@sd")
        self.assertEqual(match("Prima.cz@SD", "Prima"), "prima.cz@sd")
        # Nova.sk has no programmes and shares a name with Nova.cz: it must match nothing.
        self.assertIsNone(match("Nova.sk@SD", "Nova"))
        display = [by_id[i].findtext("display-name") for i in ("nova.cz@sd", "nova.sk@sd")]
        self.assertEqual(len({name_key(d) for d in display}), 2)

    def test_accented_and_double_escaped_guide_ids_match(self):
        # iptv-epg.org publishes "ČT1.cz"; free-epg.de double-escapes it to the
        # literal text "&#x10C;T1.cz". Both should match the playlist's CT1.cz@SD.
        playlist = (
            '#EXTM3U\n#EXTINF:-1 tvg-id="CT1.cz@SD",CT1\nhttps://cdn.example.org/ct1.m3u8\n'
            '#EXTINF:-1 tvg-id="CT2.cz@SD",CT2\nhttps://cdn.example.org/ct2.m3u8\n'
        )
        self.up.content["https://a/a.m3u"] = playlist.encode()
        self.refresh(1)
        self.engine.set_selection(["id:ct1.cz@sd", "id:ct2.cz@sd"], [])
        self.add_guide(
            3, "https://g/3.xml.gz", 10, guide({"&#x10C;T1.cz": [(xt(1), xt(2), "Zprávy")]})
        )
        self.add_guide(
            4, "https://g/4.xml.gz", 20, guide({"&amp;#x10C;T2.cz": [(xt(1), xt(2), "Film")]})
        )
        self.engine._execute({3, 4}, False, False)
        ids, progs = self.epg()
        self.assertEqual(sorted(progs), [("CT1.cz@SD", "Zprávy"), ("CT2.cz@SD", "Film")])
        self.assertEqual(ids, ["CT1.cz@SD", "CT2.cz@SD"])
        names = {r["epg_id"] for r in self.db.query("SELECT epg_id FROM epg_channels")}
        self.assertEqual(names, {"ČT1.cz", "ČT2.cz"})  # stored decoded, for display and mapping

    def test_existing_mapping_still_matches_after_normalisation(self):
        self.refresh()
        self.engine.set_selection(["id:topgear.uk@sd"], [])
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO epg_mappings (channel_key, epg_id) VALUES ('id:topgear.uk@sd', 'ČT Sport.cz')"
            )
        self.add_guide(
            3, "https://g/3.xml.gz", 10, guide({"&#x10C;T Sport.cz": [(xt(1), xt(2), "Hokej")]})
        )
        self.engine._execute({3}, False, False)
        self.assertEqual(self.epg()[1], [("TopGear.uk@SD", "Hokej")])

    def test_mapping_picked_from_a_guide_prefers_that_guide(self):
        self.refresh()
        self.engine.set_selection(["id:topgear.uk@sd"], [])
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"PRIMA.cz": [(xt(1), xt(2), "From 3")]}))
        self.add_guide(4, "https://g/4.xml.gz", 20, guide({"PRIMA.cz": [(xt(1), xt(2), "From 4")]}))
        self.engine._execute({3, 4}, False, False)
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO epg_mappings (channel_key, epg_id, source_id)"
                " VALUES ('id:topgear.uk@sd', 'PRIMA.cz', 4)"
            )
        self.engine.build_epg()
        self.assertEqual(self.epg()[1], [("TopGear.uk@SD", "From 4")])
        self.assertEqual(self.db.get_kv("epg_coverage")["id:topgear.uk@sd"]["source_id"], 4)
        # The picked guide disabled: the other guide still covers the channel.
        with self.db.conn() as c:
            c.execute("UPDATE sources SET enabled = 0 WHERE id = 4")
        self.engine.build_epg()
        self.assertEqual(self.epg()[1], [("TopGear.uk@SD", "From 3")])
        # The picked guide deleted: the mapping keeps its id and uses priority order.
        with self.db.conn() as c:
            c.execute("DELETE FROM sources WHERE id = 4")
        row = self.db.one("SELECT epg_id, source_id FROM epg_mappings")
        self.assertEqual(tuple(row), ("PRIMA.cz", None))

    def test_cached_fragments_rescanned_when_normalisation_changes(self):
        self.refresh()
        self.engine.set_selection(["id:nbatv.us@sd"], [])
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"NBATV.us": [(xt(1), xt(2), "Game")]}))
        self.engine._execute({3}, False, False)
        with self.db.conn() as c:  # simulate state written by an older version
            c.execute("UPDATE sources SET fragment_key = 'old' WHERE id = 3")
            c.execute("UPDATE epg_channels SET last_stop = NULL")
        calls = len(self.up.calls)
        self.engine.build_epg()
        self.assertEqual(self.epg()[1], [("NBATV.us@SD", "Game")])
        self.assertIsNotNone(self.db.one("SELECT last_stop FROM epg_channels")["last_stop"])
        self.assertEqual(len(self.up.calls), calls)

    def test_diagnostics_explain_missing_guide(self):
        playlist = (
            '#EXTM3U\n#EXTINF:-1 tvg-id="KinoBarrandov.cz@SD",Kino Barrandov\nhttps://x.example.org/1.m3u8\n'
            '#EXTINF:-1 tvg-id="CT24.cz@SD",CT24\nhttps://x.example.org/2.m3u8\n'
            '#EXTINF:-1 tvg-id="Stale.cz@SD",Stale\nhttps://x.example.org/3.m3u8\n'
        )
        self.up.content["https://a/a.m3u"] = playlist.encode()
        self.refresh(1)
        self.engine.set_selection(["id:kinobarrandov.cz@sd", "id:ct24.cz@sd", "id:stale.cz@sd"], [])
        text = guide(
            {"BARRANDOVKINO.cz": [(xt(1), xt(2), "Film")], "Stale.cz": [(xt(-90), xt(-89), "Old")]}
        )
        text = text.replace("BARRANDOVKINO.cz name", "CZ - BARRANDOV KINO")
        self.add_guide(3, "https://g/3.xml.gz", 10, text)
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO sources (id, kind, name, url, priority, created_at, last_error)"
                " VALUES (5, 'xmltv', 'Broken', 'https://g/5.xml', 30, 0, 'HTTP 404')"
            )
        self.engine._execute({3}, False, False)
        catalog = self.engine.catalog()
        diag = self.engine.diagnose(
            [catalog["id:kinobarrandov.cz@sd"], catalog["id:ct24.cz@sd"], catalog["id:stale.cz@sd"]]
        )
        kino = diag["id:kinobarrandov.cz@sd"]
        self.assertEqual(kino["found"], [])
        self.assertEqual(kino["suggestions"][0]["epg_id"], "BARRANDOVKINO.cz")
        self.assertEqual(diag["id:ct24.cz@sd"]["suggestions"], [])
        from aiolineup.engine import _name_tokens, _token_hits

        self.assertEqual(_token_hits(_name_tokens("CT24"), _name_tokens("ČT2")), 0)
        self.assertEqual(
            _token_hits(_name_tokens("National Geographic Wild"), _name_tokens("NAT. GEO. WILD")), 3
        )
        stale = diag["id:stale.cz@sd"]["found"]
        self.assertEqual(
            [(f["epg_id"], f["programmes"], f["usable"]) for f in stale], [("Stale.cz", 1, False)]
        )
        sources = {s["name"]: s for s in self.engine.guide_source_status()}
        self.assertEqual(sources["G3"]["channels"], 2)
        self.assertEqual(sources["Broken"]["error"], "HTTP 404")
        self.assertFalse(sources["Broken"]["cached"])

    def test_schedule_is_stored_and_reports_now_playing(self):
        self.add_guide(
            3,
            "https://g/3.xml.gz",
            10,
            guide(
                {
                    "Any.cz": [
                        (xt(-2), xt(-1), "Old"),
                        (xt(-0.5), xt(0.5), "Now on"),
                        (xt(1), xt(2), "Later"),
                    ]
                }
            ),
        )
        self.engine._execute({3}, False, False)
        titles = [
            r["title"] for r in self.db.query("SELECT title FROM epg_schedule ORDER BY start")
        ]
        self.assertEqual(titles, ["Now on", "Later"])
        items = [{"source_id": 3, "epg_id": "ANY.cz"}, {"source_id": 3, "epg_id": "Missing.cz"}]
        self.engine.now_playing(items)
        self.assertEqual(items[0]["now"]["title"], "Now on")
        self.assertIsNone(items[1]["now"])
        self.assertFalse(list(self.env.cfg.cache_dir.glob("*.schedule.tmp")))
        # A re-scan replaces the source's rows instead of adding to them.
        self.engine.build_epg(force_scan=True)
        self.assertEqual(self.db.one("SELECT count(*) FROM epg_schedule")[0], 2)

    def test_unchanged_guide_rescans_when_schedule_is_old(self):
        self.add_guide(
            3, "https://g/3.xml.gz", 10, guide({"Any.cz": [(xt(-0.5), xt(0.5), "Now on")]})
        )
        self.engine._execute({3}, False, False)
        with self.db.conn() as c:
            c.execute("DELETE FROM epg_schedule")
        # Fresh schedule: a 304 changes nothing and the rebuild does not re-scan.
        self.engine._execute({3}, False, False)
        self.assertEqual(self.db.one("SELECT count(*) FROM epg_schedule")[0], 0)
        old = time.time() - engine_mod.SCHEDULE_STALE_SECONDS - 60
        self.db.set_kv("schedule_scanned", {"3": old})
        calls = len(self.up.calls)
        self.engine._execute({3}, False, False)
        self.assertEqual(len(self.up.calls), calls + 1)  # one conditional request, answered 304
        self.assertEqual(self.db.one("SELECT count(*) FROM epg_schedule")[0], 1)
        self.assertGreater(self.db.get_kv("schedule_scanned")["3"], old)

    def test_schema_v3_database_rescans_cached_guides(self):
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO sources (id, kind, name, url, created_at, fragment_key)"
                " VALUES (9, 'xmltv', 'G', 'https://g', 0, 'abc')"
            )
            c.execute("INSERT INTO epg_mappings VALUES ('id:x', 'Keep.me', 9)")
            c.execute("DROP TABLE epg_schedule")
            c.execute("PRAGMA user_version = 3")
        self.env.db.close()
        self.engine = self.env.reopen()
        self.db = self.env.db
        self.assertEqual(self.db.one("PRAGMA user_version")[0], SCHEMA_VERSION)
        self.assertEqual(self.db.one("SELECT count(*) FROM epg_schedule")[0], 0)
        self.assertIsNone(self.db.one("SELECT fragment_key FROM sources WHERE id = 9")[0])
        self.assertEqual(
            tuple(self.db.one("SELECT epg_id, source_id FROM epg_mappings")), ("Keep.me", 9)
        )

    def test_schema_v1_database_is_migrated(self):
        import sqlite3

        self.env.db.close()
        raw = sqlite3.connect(self.env.cfg.db_path)
        raw.executescript(
            "DROP TABLE epg_channels; CREATE TABLE epg_channels (source_id INTEGER NOT NULL,"
            " epg_id TEXT NOT NULL, epg_id_lc TEXT NOT NULL, display_name TEXT NOT NULL,"
            " programmes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (source_id, epg_id_lc));"
            " DROP TABLE epg_mappings; CREATE TABLE epg_mappings (channel_key TEXT PRIMARY KEY,"
            " epg_id TEXT NOT NULL);"
            " INSERT INTO epg_mappings VALUES ('id:x', 'Keep.me'); PRAGMA user_version = 1;"
        )
        raw.close()
        engine = self.env.reopen()
        self.db = self.env.db
        cols = {r[1] for r in self.db.query("PRAGMA table_info(epg_channels)")}
        self.assertTrue({"first_start", "last_stop"} <= cols)
        self.assertEqual(self.db.one("PRAGMA user_version")[0], SCHEMA_VERSION)
        cols = {r[1] for r in self.db.query("PRAGMA table_info(epg_mappings)")}
        self.assertIn("source_id", cols)
        self.assertEqual(
            self.db.one("SELECT epg_id FROM epg_mappings WHERE channel_key = 'id:x'")[0], "Keep.me"
        )
        self.assertEqual(self.db.one("SELECT count(*) FROM sources")[0], 2)  # not re-seeded
        self.engine = engine

    def test_selection_change_refilters_cached_guide_without_download(self):
        self.refresh()
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"NBATV.us": [(xt(1), xt(2), "Game")]}))
        self.engine._execute({3}, False, False)
        self.assertEqual(self.epg()[1], [])
        calls = len(self.up.calls)
        self.engine.set_selection(["id:nbatv.us@sd"], [])
        self.engine.build_epg()
        self.assertEqual(self.epg()[1], [("NBATV.us@SD", "Game")])
        self.assertEqual(len(self.up.calls), calls)  # served from the cached raw guide

    def test_source_delete_cleans_up(self):
        self.refresh()
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"NBATV.us": [(xt(1), xt(2), "Game")]}))
        self.engine._execute({3}, False, False)
        self.assertTrue(self.engine.raw_path(3).exists())
        self.assertEqual(
            self.db.one("SELECT count(*) n FROM epg_channels WHERE source_id = 3")["n"], 1
        )
        with self.db.conn() as c:
            c.execute("DELETE FROM sources WHERE id = 3")
        self.assertEqual(self.db.one("SELECT count(*) n FROM epg_channels")["n"], 0)  # cascade

    # ------------------------------------------------------------ lineup: numbers, streams, edits

    RB, TG, SP, NBA = (
        "id:redbulltv.at@sd",
        "id:topgear.uk@sd",
        "name:speed sport 1",
        "id:nbatv.us@sd",
    )
    RB_1080 = m3u.signature_key("https://cdn.example.org/redbull/1080.m3u8", [])
    RB_720 = m3u.signature_key(
        "https://cdn.example.org/redbull/720.m3u8", ["#EXTVLCOPT:http-user-agent=Mozilla/5.0"]
    )
    RB_BACKUP = m3u.signature_key("https://backup.example.org/redbull.m3u8", [])

    def numbers(self) -> dict[str, int]:
        return self.engine.selection_numbers()

    def lineup(self) -> list[tuple[str, str, str]]:
        """(tvg-chno, tvg-id, title) per channel, in feed order."""
        self.engine.build_playlist()
        out = []
        for e in self.playlist():
            row = (e.attr("tvg-chno"), e.tvg_id, e.title)
            if row not in out:
                out.append(row)
        return out

    def test_new_selections_take_the_next_numbers_in_the_order_given(self):
        self.refresh()
        self.engine.set_selection([self.TG, self.RB], [])
        self.engine.set_selection([self.SP, self.RB], [])  # RB already has one
        self.assertEqual(self.numbers(), {self.TG: 1, self.RB: 2, self.SP: 3})
        self.engine.set_number(self.TG, 40)
        self.engine.set_selection([self.NBA], [])
        self.assertEqual(self.numbers()[self.NBA], 41)  # highest + 1, gaps stay

    def test_feed_is_sorted_by_number_and_carries_tvg_chno(self):
        self.refresh()
        self.engine.set_selection([self.RB, self.TG, self.SP], [])
        self.engine.set_number(self.SP, 7)
        self.engine.set_number(self.RB, 12)
        self.assertEqual(
            self.lineup(),
            [
                ("2", "TopGear.uk@SD", "Top Gear"),
                ("7", "Speed Sport 1", "Speed Sport 1"),
                ("12", "RedBullTV.at@SD", "Red Bull TV"),
            ],
        )
        # Every stream of a channel carries its number.
        self.assertEqual([e.attr("tvg-chno") for e in self.playlist()].count("12"), 3)

    def test_a_used_number_is_refused_and_nothing_renumbers(self):
        self.refresh()
        self.engine.set_selection([self.RB, self.TG], [])
        with self.assertRaisesRegex(ValueError, r"^Number 1 is used by Red Bull TV\.$"):
            self.engine.set_number(self.TG, 1)
        self.engine.set_edit(self.RB, name="Red Bull")
        with self.assertRaisesRegex(ValueError, r"^Number 1 is used by Red Bull\.$"):
            self.engine.set_number(self.TG, 1)
        self.assertEqual(self.numbers(), {self.RB: 1, self.TG: 2})
        self.engine.set_number(self.RB, 1)  # its own number: no change, no error
        with self.assertRaises(KeyError):
            self.engine.set_number(self.SP, 5)  # not selected

    def test_missing_channel_keeps_its_number_and_the_feed_skips_it(self):
        self.refresh()
        self.engine.set_selection([self.RB, self.NBA, self.TG], [])
        with self.db.conn() as c:  # source B (the only NBA TV source) goes away
            c.execute("UPDATE sources SET enabled = 0 WHERE id = 2")
        self.engine.invalidate_catalog()
        self.assertEqual([r[0] for r in self.lineup()], ["1", "3"])
        with self.assertRaisesRegex(ValueError, "Number 2 is used by NBA TV"):
            self.engine.set_number(self.TG, 2)

    def test_deselect_forgets_number_and_streams_but_keeps_edits(self):
        self.refresh()
        self.engine.set_selection([self.RB, self.TG], [])
        self.engine.set_streams(self.RB, [(self.RB_BACKUP, True), (self.RB_1080, False)])
        self.engine.set_edit(self.RB, name="Red Bull", logo="https://l/rb.png")
        self.engine.set_selection([], [self.RB])
        self.assertEqual(self.db.one("SELECT count(*) FROM stream_prefs")[0], 0)
        self.engine.set_selection([self.RB], [])
        self.assertEqual(self.numbers(), {self.TG: 2, self.RB: 3})  # back at the end
        self.assertEqual(self.engine.catalog()[self.RB].feed_name, "Red Bull")
        self.engine.build_playlist()
        self.assertEqual(
            [e.url for e in self.playlist() if e.tvg_id == "RedBullTV.at@SD"][0],
            "https://cdn.example.org/redbull/1080.m3u8",  # source order again
        )

    def test_number_past_the_maximum_takes_the_lowest_free_one(self):
        self.refresh()
        self.engine.set_selection([self.RB, self.TG], [])
        self.engine.set_number(self.RB, engine_mod.MAX_NUMBER)
        self.engine.set_selection([self.SP], [])
        self.assertEqual(self.numbers()[self.SP], 1)

    def test_upstream_channel_numbers_are_dropped(self):
        self.up.content["https://a/a.m3u"] = (
            b'#EXTM3U\n#EXTINF:-1 tvg-id="X.us" TVG-CHNO="88" ch-number="77",X\nhttp://x/1\n'
        )
        self.refresh(1)
        self.engine.set_selection(["id:x.us"], [])
        self.engine.build_playlist()
        text = self.engine.playlist_path.read_text()
        self.assertEqual(text.lower().count("chno"), 1)
        self.assertNotIn("ch-number", text)
        self.assertIn('tvg-chno="1"', text)

    def test_stream_order_and_off_shape_the_feed(self):
        self.refresh()
        self.engine.set_selection([self.RB], [])
        self.engine.set_streams(self.RB, [(self.RB_BACKUP, True), (self.RB_1080, False)])
        self.engine.build_playlist()
        # Listed ones first by position (off left out); the unlisted 720 follows, on.
        self.assertEqual(
            [e.url for e in self.playlist()],
            [
                "https://backup.example.org/redbull.m3u8",
                "https://cdn.example.org/redbull/720.m3u8",
            ],
        )
        self.assertEqual(self.playlist()[1].directives, ["#EXTVLCOPT:http-user-agent=Mozilla/5.0"])
        # A stream that appears later upstream goes after the ranked ones, on.
        self.up.content["https://b/b.m3u"] += (
            b'#EXTINF:-1 tvg-id="RedBullTV.at@SD",Red Bull TV\nhttps://new.example.org/rb.m3u8\n'
        )
        self.refresh(2)
        self.engine.build_playlist()
        self.assertEqual([e.url for e in self.playlist()][-1], "https://new.example.org/rb.m3u8")
        # null goes back to source order with everything on.
        self.engine.set_streams(self.RB, None)
        self.engine.build_playlist()
        self.assertEqual(len(self.playlist()), 4)
        self.assertEqual(self.playlist()[0].url, "https://cdn.example.org/redbull/1080.m3u8")

    def test_at_least_one_present_stream_stays_on(self):
        self.refresh()
        self.engine.set_selection([self.RB], [])
        all_off = [(s, False) for s in (self.RB_1080, self.RB_720, self.RB_BACKUP)]
        with self.assertRaisesRegex(ValueError, "At least one stream must stay on"):
            self.engine.set_streams(self.RB, all_off)
        # A stream only in a disabled source does not count as present.
        with self.db.conn() as c:
            c.execute("UPDATE sources SET enabled = 0 WHERE id = 2")
        with self.assertRaisesRegex(ValueError, "At least one stream must stay on"):
            self.engine.set_streams(
                self.RB, [(self.RB_1080, False), (self.RB_720, False), (self.RB_BACKUP, True)]
            )
        self.assertEqual(self.db.one("SELECT count(*) FROM stream_prefs")[0], 0)
        for bad in ([("nope", True)], [(self.RB_1080, True), (self.RB_1080, False)]):
            with self.assertRaises(ValueError):
                self.engine.set_streams(self.RB, bad)
        with self.assertRaises(KeyError):
            self.engine.set_streams(self.TG, [])  # not selected

    def test_a_save_keeps_choices_for_streams_the_editor_cannot_see(self):
        self.refresh()
        self.engine.set_selection([self.RB], [])
        self.engine.set_streams(self.RB, [(self.RB_BACKUP, False), (self.RB_1080, True)])
        with self.db.conn() as c:  # the backup's only playlist (B) is switched off
            c.execute("UPDATE sources SET enabled = 0 WHERE id = 2")
        self.engine.invalidate_catalog()
        shown = [sig for sig, _, _ in self.engine.channel_streams(self.RB)]
        self.assertNotIn(self.RB_BACKUP, shown)
        self.engine.set_streams(self.RB, [(self.RB_720, True), (self.RB_1080, True)])
        with self.db.conn() as c:
            c.execute("UPDATE sources SET enabled = 1 WHERE id = 2")
        self.engine.invalidate_catalog()
        self.engine.build_playlist()
        # Back with B: the backup is still off, after the order saved without it.
        self.assertEqual(
            [(sig, on) for sig, _, on in self.engine.channel_streams(self.RB)],
            [(self.RB_720, True), (self.RB_1080, True), (self.RB_BACKUP, False)],
        )
        self.assertNotIn("backup", self.engine.playlist_path.read_text())
        self.engine.set_streams(self.RB, None)  # source order forgets it too
        self.assertEqual(self.db.one("SELECT count(*) FROM stream_prefs")[0], 0)

    def test_playlist_stats_count_only_channels_the_file_lists(self):
        self.refresh()
        self.engine.set_selection([self.RB, self.TG], [])
        self.engine.set_streams(
            self.RB, [(self.RB_BACKUP, True), (self.RB_1080, False), (self.RB_720, False)]
        )
        with self.db.conn() as c:  # the only on stream goes; the off ones stay
            c.execute("UPDATE sources SET enabled = 0 WHERE id = 2")
        self.engine.invalidate_catalog()
        stats = self.engine.build_playlist()
        self.assertEqual({e.tvg_id for e in self.playlist()}, {"TopGear.uk@SD"})
        self.assertEqual(stats["channels"], 1)

    def test_own_name_and_logo_reach_both_feeds_and_reset(self):
        import xml.etree.ElementTree as ET

        self.refresh()
        self.engine.set_selection([self.RB, self.SP], [])
        self.engine.set_edit(self.RB, name="Red Bull, Österreich", logo="https://my/rb.png")
        self.engine.set_edit(self.SP, logo="https://my/sp.png")
        self.engine.build_playlist()
        self.engine.build_epg()
        entries = self.playlist()
        rb = [e for e in entries if e.tvg_id == "RedBullTV.at@SD"]
        self.assertEqual({e.title for e in rb}, {"Red Bull, Österreich"})
        self.assertEqual({e.attr("tvg-logo") for e in rb}, {"https://my/rb.png"})
        self.assertEqual(self.engine.playlist_path.read_text().count("example.org/rb.png"), 0)
        sp = [e for e in entries if e.tvg_id == "Speed Sport 1"]
        self.assertEqual({e.title for e in sp}, {"Speed Sport 1"})  # name untouched
        root = ET.parse(self.engine.epg_path).getroot()
        shown = {
            c.get("id"): (c.findtext("display-name"), c.find("icon").get("src"))
            for c in root.findall("channel")
        }
        self.assertEqual(
            shown,
            {
                "RedBullTV.at@SD": ("Red Bull, Österreich", "https://my/rb.png"),
                "Speed Sport 1": ("Speed Sport 1", "https://my/sp.png"),
            },
        )
        # The exported id and guide matching keep the source name.
        self.assertEqual(self.engine.catalog()[self.SP].candidates(), ["speed sport 1"])
        # Empty resets one field and keeps the other.
        self.engine.set_edit(self.RB, logo="")
        self.assertEqual(
            tuple(
                self.db.one(
                    "SELECT name, logo FROM channel_edits WHERE channel_key = ?", (self.RB,)
                )
            ),
            ("Red Bull, Österreich", None),
        )
        self.engine.set_edit(self.RB, name=None)
        self.assertIsNone(
            self.db.one("SELECT 1 FROM channel_edits WHERE channel_key = ?", (self.RB,))
        )
        self.engine.build_playlist()
        self.assertEqual(self.playlist()[0].title, "Red Bull TV")
        self.assertEqual(self.playlist()[0].attr("tvg-logo"), "https://example.org/rb.png")

    def test_edits_queue_rebuilds_only_for_selected_channels(self):
        self.refresh()
        self.engine.set_selection([self.RB], [])
        with (
            mock.patch.object(self.engine, "request_playlist") as rp,
            mock.patch.object(self.engine, "request_epg") as re_,
        ):
            self.engine.set_edit(self.TG, name="Gear")  # not selected: kept, no rebuild
            rp.assert_not_called()
            re_.assert_not_called()
            self.engine.set_edit(self.RB, name="RB")
            rp.assert_called_once_with()
            re_.assert_called_once_with(delay=engine_mod.EPG_DEBOUNCE_SECONDS)
            rp.reset_mock()
            re_.reset_mock()
            self.engine.set_number(self.RB, 5)
            self.engine.set_streams(self.RB, [(self.RB_720, True)])
            self.assertEqual(rp.call_count, 2)
            re_.assert_not_called()  # numbers and streams are not in the guide
        self.assertIn("gear", self.engine.catalog()[self.TG].search)
        self.assertIn("top gear", self.engine.catalog()[self.TG].search)

    def test_renumbering_never_changes_exported_ids(self):
        # Two channels whose ids clash: the earlier-added one keeps the plain id.
        self.up.content["https://a/a.m3u"] += b"#EXTINF:-1,RedBullTV.at@SD\nhttp://n/1\n"
        self.refresh()
        clash = "name:redbulltv.at@sd"
        self.engine.set_selection([self.RB], [])
        self.engine.set_selection([clash], [])
        before = self.engine.exported_ids()
        self.engine.set_number(clash, 7)
        self.engine.set_number(self.RB, 900)
        self.engine.set_edit(self.RB, name="Renamed")
        self.assertEqual(self.engine.exported_ids(), before)
        lineup = self.lineup()
        self.assertEqual([r[0] for r in lineup], ["7", "900"])
        self.assertEqual(lineup[1][1:], ("RedBullTV.at@SD", "Renamed"))
        self.assertEqual(lineup[0][1], before[clash])
        self.assertNotEqual(before[clash], "RedBullTV.at@SD")

    # ------------------------------------------------------------ output ids, refresh safety

    def test_every_output_stream_carries_the_channels_out_id(self):
        # Upstream ids in another case (here the B playlist's "redbulltv.at@SD", and an
        # upper-case TVG-ID attribute name) are replaced by the channel's one id.
        self.up.content["https://a/a.m3u"] += b'#EXTINF:-1 TVG-ID="Up.us@SD",Upper\nhttp://u/1\n'
        self.refresh()
        self.engine.set_selection(["id:redbulltv.at@sd", "id:up.us@sd"], [])
        self.engine.build_playlist()
        entries = self.playlist()
        self.assertEqual(len(entries), 4)
        self.assertEqual(sorted({e.tvg_id for e in entries}), ["RedBullTV.at@SD", "Up.us@SD"])
        self.assertEqual(self.engine.playlist_path.read_text().lower().count("tvg-id="), 4)

    def test_failed_stream_keeps_old_rows(self):
        import gzip

        self.refresh(1)
        old = self.db.query("SELECT pos, url FROM streams WHERE source_id = 1 ORDER BY pos")
        self.assertEqual(len(old), 4)
        big = gzip.compress(PLAYLIST_A.replace("A", "Z").encode() * 50)
        for bad in (big[: len(big) // 2], b"\x1f\x8b" + b"junk" * 20):  # cut off, corrupt
            self.up.content["https://a/a.m3u"] = bad
            self.refresh(1)
            src = self.db.one("SELECT * FROM sources WHERE id = 1")
            self.assertTrue(src["last_error"])
            self.assertEqual(
                self.db.query("SELECT pos, url FROM streams WHERE source_id = 1 ORDER BY pos"), old
            )
        self.assertEqual(list(self.env.cfg.cache_dir.glob("m3u-*")), [])

    def test_gzipped_playlist_and_decompressed_limit(self):
        import gzip

        self.up.content["https://a/a.m3u"] = gzip.compress(PLAYLIST_A.encode())
        self.refresh(1)
        self.assertEqual(self.db.one("SELECT item_count n FROM sources WHERE id = 1")["n"], 4)
        bomb = gzip.compress(b"#EXTM3U\n" + b"# padding " * 3_000_000)  # 30 MB in 30 KB
        self.up.content["https://a/a.m3u"] = bomb
        self.refresh(1)
        self.assertIn(
            "limit", self.db.one("SELECT last_error FROM sources WHERE id = 1")["last_error"]
        )
        self.assertEqual(self.db.one("SELECT count(*) n FROM streams WHERE source_id = 1")["n"], 4)

    def test_source_deleted_during_refresh(self):
        self.refresh(1)
        inner = self.up.download_to_file

        def delete_then_download(url, dest, **kw):
            result = inner(url, dest, **kw)
            with self.db.conn() as c:
                c.execute("DELETE FROM sources WHERE id = 1")
            self.engine.raw_path(1).write_bytes(b"left behind")
            return result

        src = {**self.db.one("SELECT * FROM sources WHERE id = 1"), "etag": None}
        with mock.patch.object(fetch, "download_to_file", delete_then_download):
            self.assertFalse(self.engine.refresh_m3u(src))
            self.engine._execute(set(), False, False)
        self.assertEqual(self.db.one("SELECT count(*) n FROM streams WHERE source_id = 1")["n"], 0)
        self.assertEqual(list(self.env.cfg.cache_dir.iterdir()), [])  # swept, none left behind

    def test_source_id_reused_during_refresh_is_not_overwritten(self):
        self.refresh(1)
        inner = self.up.download_to_file

        def replace_source(url, dest, **kw):
            result = inner(url, dest, **kw)
            with self.db.conn() as c:  # delete, then a new source takes the same id
                c.execute("DELETE FROM sources WHERE id = 1")
                c.execute(
                    "INSERT INTO sources (id, kind, name, url, created_at)"
                    " VALUES (1, 'm3u', 'New', 'https://n/n.m3u', ?)",
                    (time.time() + 5,),
                )
            return result

        src = {**self.db.one("SELECT * FROM sources WHERE id = 1"), "etag": None}
        with mock.patch.object(fetch, "download_to_file", replace_source):
            self.assertFalse(self.engine.refresh_m3u(src))
        self.assertEqual(self.db.one("SELECT count(*) n FROM streams WHERE source_id = 1")["n"], 0)
        self.assertIsNone(self.db.one("SELECT etag FROM sources WHERE id = 1")["etag"])

    def test_guide_deleted_during_refresh_is_swept(self):
        self.refresh()
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"NBATV.us": [(xt(1), xt(2), "G")]}))
        self.engine.set_selection(["id:nbatv.us@sd"], [])
        self.engine._execute({3}, False, False)
        self.assertTrue(self.engine.raw_path(3).exists())
        self.assertTrue(self.engine.fragment_path(3).exists())
        with self.db.conn() as c:
            c.execute("DELETE FROM sources WHERE id = 3")
        self.engine._execute(set(), False, False)
        self.assertEqual(list(self.env.cfg.cache_dir.iterdir()), [])

    def test_failed_file_swap_clears_keys_so_the_next_build_rescans(self):
        self.refresh()
        self.engine.set_selection(["id:nbatv.us@sd"], [])
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"NBATV.us": [(xt(1), xt(2), "G")]}))
        self.engine._execute({3}, False, False)
        self.assertIsNotNone(self.db.one("SELECT fragment_key FROM sources WHERE id = 3")[0])
        real = engine_mod.os.replace
        calls = []

        def fail_once(a, b):
            calls.append(a)
            if len(calls) == 1:
                raise OSError("disk full")
            return real(a, b)

        # refresh_xmltv: the first replace (raw guide) fails after the scan committed.
        src = {**self.db.one("SELECT * FROM sources WHERE id = 3"), "etag": None}
        with mock.patch.object(engine_mod.os, "replace", fail_once):
            self.assertTrue(self.engine.refresh_xmltv(src))  # no OSError escapes
        row = self.db.one("SELECT * FROM sources WHERE id = 3")
        self.assertIsNone(row["fragment_key"])
        self.assertIsNone(row["etag"])
        self.assertIn("Could not save guide", row["last_error"])
        # build_epg: a failed fragment swap also clears the key.
        self.engine.build_epg()  # re-scans and restores the key
        self.assertIsNotNone(self.db.one("SELECT fragment_key FROM sources WHERE id = 3")[0])
        calls.clear()
        with mock.patch.object(engine_mod.os, "replace", fail_once):
            stats = self.engine.build_epg(force_scan=True)
        self.assertIn(3, stats["errors"])
        self.assertIsNone(self.db.one("SELECT fragment_key FROM sources WHERE id = 3")[0])

    def test_language_result_is_discarded_when_disabled_during_download(self):
        self.refresh()  # lookup still off here
        self.db.set_kv("settings", {"enrich_languages": True})
        feeds = json.dumps([{"channel": "NBATV.us", "feed": "SD", "languages": ["eng"]}])
        self.up.content[self.db.settings()["metadata_url"]] = feeds.encode()
        inner = self.up.download_to_file

        def disable_then_download(url, dest, **kw):
            result = inner(url, dest, **kw)
            self.db.set_kv("settings", {"enrich_languages": False})
            return result

        with mock.patch.object(fetch, "download_to_file", disable_then_download):
            self.engine.refresh_languages()
        self.assertEqual(self.db.one("SELECT count(*) n FROM meta_languages")["n"], 0)
        self.assertFalse(self.db.get_kv("metadata_state"))
        # A late error is discarded too.
        self.db.set_kv("settings", {"enrich_languages": True})
        self.up.content[self.db.settings()["metadata_url"]] = fetch.FetchError("HTTP 500")

        def disable_then_fail(url, dest, **kw):
            self.db.set_kv("settings", {"enrich_languages": False})
            return inner(url, dest, **kw)

        with mock.patch.object(fetch, "download_to_file", disable_then_fail):
            self.engine.refresh_languages()
        self.assertFalse(self.db.get_kv("metadata_state"))

    def test_guide_source_id_reused_during_refresh_is_not_overwritten(self):
        self.refresh()
        self.engine.set_selection(["id:nbatv.us@sd"], [])
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"NBATV.us": [(xt(1), xt(2), "G")]}))
        inner = self.up.download_to_file

        def replace_source(url, dest, **kw):
            result = inner(url, dest, **kw)
            with self.db.conn() as c:
                c.execute("DELETE FROM sources WHERE id = 3")
                c.execute(
                    "INSERT INTO sources (id, kind, name, url, created_at)"
                    " VALUES (3, 'xmltv', 'New', 'https://n/n.xml', ?)",
                    (time.time() + 5,),
                )
            return result

        src = {**self.db.one("SELECT * FROM sources WHERE id = 3"), "etag": None}
        with mock.patch.object(fetch, "download_to_file", replace_source):
            self.assertFalse(self.engine.refresh_xmltv(src))
        self.assertEqual(self.db.one("SELECT count(*) n FROM epg_channels")["n"], 0)
        self.assertIsNone(self.db.one("SELECT fragment_key FROM sources WHERE id = 3")[0])
        self.assertFalse(self.engine.raw_path(3).exists())

    def test_build_epg_survives_a_guide_that_cannot_be_read(self):
        self.refresh()
        self.engine.set_selection(["id:nbatv.us@sd"], [])
        self.add_guide(3, "https://g/3.xml.gz", 10, guide({"NBATV.us": [(xt(1), xt(2), "G")]}))
        self.engine._execute({3}, False, False)
        good = self.engine.fragment_path(3).read_bytes()
        rows = self.db.query("SELECT * FROM epg_channels WHERE source_id = 3")
        for exc in (
            fetch.SizeLimitExceeded("Decompressed guide exceeds the limit."),
            sqlite3.OperationalError("disk I/O error"),
            OSError("no space left"),
        ):
            with mock.patch.object(engine_mod.xmltv, "scan", side_effect=exc):
                stats = self.engine.build_epg(force_scan=True)
            self.assertEqual(stats["errors"], {3: str(exc)})
            self.assertEqual(self.engine.fragment_path(3).read_bytes(), good)
            self.assertEqual(self.db.query("SELECT * FROM epg_channels WHERE source_id = 3"), rows)
            self.assertEqual(self.epg()[1], [("NBATV.us@SD", "G")])  # last good programmes stay

    def test_metadata_error_is_raw_and_cleared_when_disabled(self):
        self.db.set_kv("settings", {"enrich_languages": True})
        self.refresh()
        self.engine.refresh_languages()  # the fake upstream answers HTTP 404
        error = self.db.get_kv("metadata_state")["error"]
        self.assertEqual(error, "HTTP 404")  # the UI adds its own "Language metadata:" prefix
        self.db.set_kv("settings", {"enrich_languages": False})
        self.engine.refresh_languages()
        self.assertFalse(self.db.get_kv("metadata_state"))  # etag gone too: re-enable refetches


class StreamingMemoryTests(unittest.TestCase):
    def test_m3u_refresh_streams_in_flat_memory(self):
        # A ~20 MiB playlist (long logo URLs keep the entry count down) must not be held
        # in memory: download, decode, parse and insert all stream.
        import tracemalloc

        logo = "https://img.example.org/" + "x" * 3900
        lines = ["#EXTM3U"]
        for i in range(5_000):
            lines.append(
                f'#EXTINF:-1 tvg-id="C{i}.us" tvg-logo="{logo}/{i}" group-title="G",Chan {i}'
            )
            lines.append(f"https://cdn.example.org/{i}.m3u8")
        data = "\n".join(lines).encode()
        self.assertGreater(len(data), 19 * 2**20)
        env = TempEnv(M3U_MAX_MB="32")
        try:
            with env.db.conn() as c:
                c.execute("DELETE FROM sources")
                c.execute(
                    "INSERT INTO sources (id, kind, name, url, created_at) VALUES (1, 'm3u', 'A', 'https://a/a.m3u', 0)"
                )
            src = env.db.one("SELECT * FROM sources WHERE id = 1")
            with FakeUpstream() as up:
                up.content["https://a/a.m3u"] = data
                tracemalloc.start()
                try:
                    self.assertTrue(env.engine.refresh_m3u(src))
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
            self.assertEqual(env.db.one("SELECT count(*) n FROM streams")["n"], 5_000)
            self.assertLess(peak, 8 * 2**20, f"peak {peak / 2**20:.1f} MiB")
        finally:
            env.close()


class WorkerTests(unittest.TestCase):
    def test_requests_coalesce_and_run_serially(self):
        env = TempEnv()
        try:
            engine = env.engine
            calls = []
            running = []

            def fake_execute(ids, playlist, metadata):
                running.append(1)
                self.assertEqual(len(running), 1)  # never overlapping
                calls.append((set(ids), playlist))
                time.sleep(0.05)
                running.pop()

            with mock.patch.object(engine, "_execute", fake_execute):
                engine.start()
                for _ in range(5):
                    engine.request_refresh([1])
                    engine.request_playlist()
                deadline = time.time() + 3
                while time.time() < deadline and (not calls or engine.pending()["refresh"]):
                    time.sleep(0.02)
                engine.stop()
            self.assertLessEqual(len(calls), 2)
            self.assertEqual(set().union(*(c[0] for c in calls)), {1})
        finally:
            env.close()


class FeedWarningTests(unittest.TestCase):
    def test_each_aiostreams_limit_warns_once(self):
        self.assertEqual(
            engine_mod.feed_warnings(
                {"size_bytes": 16_000_000, "streams": 10_000},
                {"size_bytes": 128_000_000, "programmes": 200_000},
            ),
            [],
        )
        over = engine_mod.feed_warnings(
            {"size_bytes": 16_000_001, "streams": 10_001},
            {"size_bytes": 128_000_001, "programmes": 200_001},
        )
        self.assertEqual(len(over), 4)
        self.assertTrue(all("AIOStreams" in w for w in over))
        self.assertEqual(engine_mod.feed_warnings({}, {}), [])


class ExportedIdTests(unittest.TestCase):
    def ids(self, *plain: str) -> list[str]:
        out = engine_mod.assign_export_ids([(f"k{i}", p) for i, p in enumerate(plain)])
        return [out[f"k{i}"] for i in range(len(plain))]

    def test_ids_that_do_not_clash_stay_plain(self):
        self.assertEqual(self.ids("Nova", "Prima", "Nova@HD2"), ["Nova", "Prima", "Nova@HD2"])

    def test_id_and_name_clash_ignoring_case(self):
        self.assertEqual(self.ids("nova", "NOVA"), ["nova", "NOVA~2"])
        self.assertEqual(self.ids("nova", "Nova", "NOVA"), ["nova", "Nova~2", "NOVA~3"])

    def test_quality_fallback_clashes_in_both_orders(self):
        self.assertEqual(self.ids("Nova", "Nova@HD"), ["Nova", "Nova@HD~2"])
        self.assertEqual(self.ids("Nova@HD", "Nova"), ["Nova@HD", "Nova~2"])

    def test_two_qualities_of_one_name_do_not_clash(self):
        self.assertEqual(self.ids("Nova@HD", "Nova@SD"), ["Nova@HD", "Nova@SD"])

    def test_suffix_skips_taken_ids(self):
        self.assertEqual(self.ids("Nova~2", "Nova", "nova"), ["Nova~2", "Nova", "nova~3"])

    def test_long_id_is_cut_with_a_hash_and_stays_unique(self):
        a, b = self.ids("A" * 300, "A" * 299 + "B")
        for out in (a, b):
            self.assertEqual(len(out.encode("utf-16-le")) // 2, 249)
            self.assertTrue(out.startswith("A" * 240 + "~"))
        self.assertNotEqual(a.lower(), b.lower())
        self.assertEqual(self.ids("A" * 300, "A" * 299 + "B"), [a, b])  # deterministic

    def test_long_id_cut_counts_utf16_units(self):
        (out,) = self.ids("\U0001f4fa" * 200)  # 400 UTF-16 units
        self.assertLessEqual(len(out.encode("utf-16-le")) // 2, 256)
        self.assertTrue(out.startswith("\U0001f4fa" * 120 + "~"))

    def test_ids_equal_in_their_first_256_units_clash(self):
        a = "x" * 256
        first, second = self.ids(a, a + "y")
        self.assertEqual(first, a)
        self.assertTrue(second.startswith(a[:240] + "~"))
        self.assertLessEqual(len(second), 256)


class FeedLanguageTests(unittest.TestCase):
    def test_incremental_parse(self):
        feeds = [
            {"channel": "RedBullTV.at", "id": "SD", "is_main": True, "languages": ["eng"]},
            {"channel": "RedBullTV.at", "id": "BR", "is_main": False, "languages": ["por"]},
            {"channel": "Ignored.us", "id": "SD", "is_main": True, "languages": ["eng"]},
        ]
        text = " [\n" + ",\n".join(json.dumps(f) for f in feeds) + "\n]\n"
        wanted = {"redbulltv.at@sd", "redbulltv.at@br", "redbulltv.at"}
        expected = {"redbulltv.at@sd": ["eng"], "redbulltv.at": ["eng"], "redbulltv.at@br": ["por"]}
        for chunk in (1, 7, 64, 10_000):  # objects straddling chunk boundaries
            stream = io.StringIO(text)
            self.assertEqual(
                parse_feed_languages(lambda n, s=stream, c=chunk: s.read(c), wanted), expected
            )
        # One object that never ends is refused once the buffer passes 4 MiB.
        endless = iter(['[{"channel": "'] + ["x" * 1_000_000] * 10)
        with self.assertRaises(ValueError):
            parse_feed_languages(lambda n: next(endless, ""), wanted)
        for bad in ("", "{}", '[{"channel": ', "[1, 2"):
            stream = io.StringIO(bad)
            with self.assertRaises(ValueError, msg=bad):
                parse_feed_languages(stream.read, wanted)


if __name__ == "__main__":
    unittest.main()
