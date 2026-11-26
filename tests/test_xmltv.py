import calendar
import json
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from aiolineup import fetch, xmltv
from tests.helpers import guide, write_gz

MiB = 1024 * 1024


def ts(s: str) -> str:
    return s + " +0000"


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _guide(self):
        return guide(
            {
                "Keep.us": [(ts("20260101100000"), ts("20260101110000"), "Kept show")],
                "Drop.us": [(ts("20260101100000"), ts("20260101110000"), "Dropped")] * 3,
            }
        )

    def test_plain_and_gzip_give_same_result(self):
        plain = self.dir / "g.xml"
        plain.write_text(self._guide())
        gz = write_gz(self.dir / "g.xml.gz", self._guide())
        for raw in (plain, gz):
            frag = self.dir / "frag.xml"
            r = xmltv.scan(raw, {"keep.us"}, frag, 10 * MiB)
            self.assertEqual(r.kept, 1)
            self.assertEqual(r.index["drop.us"].programmes, 3)
            self.assertEqual(r.index["keep.us"].display_name, "Keep.us name")
            root = ET.parse(frag).getroot()
            self.assertEqual([p.get("channel") for p in root], ["Keep.us"])
            self.assertEqual(root[0].findtext("desc"), "About Kept show & more")

    def test_schedule_keeps_every_channel_inside_48_hours(self):
        now = calendar.timegm((2026, 1, 1, 12, 0, 0))
        text = guide(
            {
                "A.us": [
                    (ts("20260101100000"), ts("20260101110000"), "Ended"),
                    (ts("20260101113000"), ts("20260101123000"), "On\tair\n now"),
                    (ts("20260103115900"), ts("20260103130000"), "Edge"),
                    (ts("20260103120000"), ts("20260103130000"), "Too late"),
                ],
                "B.us": [(ts("20260101120000"), ts("20260101130000"), "Other")],
                "Odd&#10;id\tx.us": [(ts("20260101120000"), ts("20260101130000"), "Odd")],
            }
        )
        raw = self.dir / "g.xml"
        raw.write_text(text, encoding="utf-8")
        sched = self.dir / "s.jsonl"
        xmltv.scan(raw, set(), self.dir / "f.xml", 10 * MiB, schedule_path=sched, now=now)
        rows = [json.loads(line) for line in sched.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r[3] for r in rows], ["On air now", "Edge", "Other", "Odd"])
        self.assertEqual(rows[0][:3], ["a.us", now - 1800, now + 1800])
        # XML turns a literal tab in an attribute into a space; &#10; stays a newline.
        self.assertEqual(rows[3][0], xmltv.norm_id("Odd\nid x.us"))

    def test_time_span_is_kept_for_every_id_not_only_wanted_ones(self):
        raw = self.dir / "g.xml"
        raw.write_text(self._guide(), encoding="utf-8")
        for wanted in ({"keep.us"}, set()):
            res = xmltv.scan(raw, wanted, self.dir / "f.xml", 10 * MiB)
            for cid in ("keep.us", "drop.us"):
                entry = res.index[cid]
                self.assertEqual(entry.first_start, calendar.timegm((2026, 1, 1, 10, 0, 0)), cid)
                self.assertEqual(entry.last_stop, calendar.timegm((2026, 1, 1, 11, 0, 0)), cid)

    def test_invalid_documents(self):
        cases = {
            "html": "<html><body>not found</body></html>",
            "truncated": self._guide()[:-40],
            "empty": "",
            "garbage": "\x00\x01 nope",
        }
        for name, text in cases.items():
            with self.subTest(name):
                p = self.dir / f"{name}.xml"
                p.write_text(text)
                with self.assertRaises(xmltv.GuideError):
                    xmltv.scan(p, set(), self.dir / "f.xml", 10 * MiB)

    def test_truncated_gzip(self):
        gz = write_gz(self.dir / "g.gz", self._guide() * 1)
        data = gz.read_bytes()
        gz.write_bytes(data[: len(data) // 2])
        with self.assertRaises(xmltv.GuideError):
            xmltv.scan(gz, set(), self.dir / "f.xml", 10 * MiB)

    def test_corrupt_deflate_data(self):
        gz = write_gz(self.dir / "g.gz", self._guide())
        data = bytearray(gz.read_bytes())
        for i in range(10, 40):  # keep the gzip header, garble the deflate stream
            data[i] ^= 0xFF
        gz.write_bytes(bytes(data))
        with self.assertRaises(xmltv.GuideError):
            xmltv.scan(gz, set(), self.dir / "f.xml", 10 * MiB)

    def test_decompressed_size_limit(self):
        big = guide({"A.us": [(ts("20260101100000"), ts("20260101110000"), "x" * 1000)] * 3000})
        gz = write_gz(self.dir / "big.gz", big)
        self.assertLess(gz.stat().st_size, 200_000)  # compresses extremely well
        with self.assertRaises(fetch.SizeLimitExceeded):
            xmltv.scan(gz, set(), self.dir / "f.xml", 1 * MiB)

    def test_entity_expansion_bomb_is_rejected(self):
        bomb = '<?xml version="1.0"?>\n<!DOCTYPE tv [<!ENTITY a "aaaaaaaaaa">'
        prev = "a"
        for i in range(1, 10):
            bomb += f'<!ENTITY a{i} "{("&" + prev + ";") * 10}">'
            prev = f"a{i}"
        bomb += f']>\n<tv><channel id="x"><display-name>&{prev};</display-name></channel></tv>'
        p = self.dir / "bomb.xml"
        p.write_text(bomb)
        with self.assertRaises((xmltv.GuideError, fetch.SizeLimitExceeded)):
            xmltv.scan(p, set(), self.dir / "f.xml", 10 * MiB)

    def test_external_entities_not_resolved(self):
        secret = self.dir / "secret.txt"
        secret.write_text("TOPSECRET")
        doc = (
            f'<?xml version="1.0"?><!DOCTYPE tv [<!ENTITY x SYSTEM "file://{secret}">]>'
            '<tv><channel id="c"><display-name>&x;</display-name></channel></tv>'
        )
        p = self.dir / "xxe.xml"
        p.write_text(doc)
        try:
            r = xmltv.scan(p, set(), self.dir / "f.xml", 10 * MiB)
        except xmltv.GuideError:
            return
        self.assertNotIn("TOPSECRET", r.index["c"].display_name)


class AssembleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_routes_rename_dedupe_and_window(self):
        frag1 = self.dir / "1.xml"
        frag1.write_text(
            guide(
                {
                    "rb.us": [
                        (ts("20260101100000"), ts("20260101110000"), "Show A"),
                        (
                            ts("20260101100000"),
                            ts("20260101110000"),
                            "Show A again",
                        ),  # duplicate start
                        (ts("20260101110000"), ts("20260101120000"), "Show B"),
                        (ts("20250101100000"), ts("20250101110000"), "Ancient"),
                        (ts("20300101100000"), ts("20300101110000"), "Far future"),
                    ],
                }
            )
        )
        frag2 = self.dir / "2.xml"
        frag2.write_text(
            guide({"rb.us": [(ts("20260101100000"), ts("20260101110000"), "Other source")]})
        )
        out, gz = self.dir / "epg.xml", self.dir / "epg.xml.gz"
        not_before = calendar.timegm((2025, 12, 31, 0, 0, 0))
        not_after = calendar.timegm((2026, 2, 1, 0, 0, 0))
        stats = xmltv.assemble(
            [(1, frag1), (2, frag2)],
            {(1, "rb.us"): ["RedBullTV.at@US", "RedBullTV.at@UK"]},  # source 2 not routed
            [
                xmltv.OutputChannel("RedBullTV.at@US", "Red Bull US", "https://l/x.png"),
                xmltv.OutputChannel("RedBullTV.at@UK", "Red Bull UK"),
                xmltv.OutputChannel("NoGuide.us", "No guide"),
            ],
            out,
            gz,
            not_before=not_before,
            not_after=not_after,
        )
        root = ET.parse(out).getroot()
        self.assertEqual(
            [c.get("id") for c in root.findall("channel")],
            ["RedBullTV.at@US", "RedBullTV.at@UK", "NoGuide.us"],
        )
        titles = [(p.get("channel"), p.findtext("title")) for p in root.findall("programme")]
        self.assertEqual(
            titles,
            [
                ("RedBullTV.at@US", "Show A"),
                ("RedBullTV.at@UK", "Show A"),
                ("RedBullTV.at@US", "Show B"),
                ("RedBullTV.at@UK", "Show B"),
            ],
        )
        self.assertEqual(stats.duplicates, 2)
        self.assertEqual(stats.outside_window, 2)
        self.assertTrue(gz.exists())
        self.assertFalse(list(self.dir.glob("*.tmp")))

    def test_parse_time(self):
        self.assertEqual(
            xmltv.parse_time("20260101120000 +0100"), calendar.timegm((2026, 1, 1, 11, 0, 0))
        )
        self.assertEqual(
            xmltv.parse_time("20260101120000 -02:30"), calendar.timegm((2026, 1, 1, 14, 30, 0))
        )
        self.assertEqual(xmltv.parse_time("202601011200"), calendar.timegm((2026, 1, 1, 12, 0, 0)))
        self.assertIsNone(xmltv.parse_time("garbage"))
        self.assertIsNone(xmltv.parse_time(None))


class MemoryTests(unittest.TestCase):
    """Peak memory must not grow with guide size (guards against tree-building regressions)."""

    def test_large_guide_constant_memory(self):
        import tracemalloc

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gz = d / "large.xml.gz"
            import gzip

            with gzip.open(gz, "wt", encoding="utf-8") as fh:
                fh.write("<tv>\n")
                for c in range(2000):
                    fh.write(
                        f'<channel id="c{c}.us"><display-name>Channel {c}</display-name></channel>\n'
                    )
                start = int(time.time())

                def stamp(t):  # real XMLTV timestamps, so schedule rows are written too
                    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t))

                for i in range(60_000):  # ~60 MiB decompressed
                    fh.write(
                        f'<programme start="{stamp(start + i)}" stop="{stamp(start + i + 1)}" channel="c{i % 2000}.us">'
                        f"<title>Programme {i}</title><desc>{'lorem ipsum ' * 70}</desc></programme>\n"
                    )
                fh.write("</tv>\n")
            tracemalloc.start()
            r = xmltv.scan(gz, {"c1.us"}, d / "frag.xml", 1024 * MiB, schedule_path=d / "sched.tsv")
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertGreater(r.decompressed_bytes, 50 * MiB)
            self.assertEqual(r.kept, 30)
            with open(d / "sched.tsv", encoding="utf-8") as sched:
                self.assertGreater(sum(1 for _ in sched), 50_000)
            self.assertLess(peak, 8 * MiB, f"peak traced memory {peak / MiB:.1f} MiB")


if __name__ == "__main__":
    unittest.main()


class DisplayNameTests(unittest.TestCase):
    def names(self, channels):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            xmltv.assemble(
                [], {}, channels, d / "e.xml", d / "e.xml.gz", not_before=None, not_after=None
            )
            root = ET.parse(d / "e.xml").getroot()
            return [c.findtext("display-name") for c in root.findall("channel")]

    def assert_unique_lookups(self, channels, shown):
        keys = [xmltv.name_key(n) for n in shown]
        self.assertEqual(len(set(keys)), len(keys))
        for ch, k in zip(channels, keys, strict=True):
            others = {xmltv.lookup_key(o.name) for o in channels if o is not ch}
            self.assertNotIn(k, others)

    def test_colliding_names_get_the_id_and_the_rest_stay(self):
        chans = [
            xmltv.OutputChannel("a", "Nova"),
            xmltv.OutputChannel("b", "NOVA_"),
            xmltv.OutputChannel("c", "Prima"),
        ]
        shown = self.names(chans)
        self.assertEqual(shown, ["Nova (a)", "NOVA_ (b)", "Prima"])
        self.assert_unique_lookups(chans, shown)

    def test_generated_suffix_never_collides_with_another_name(self):
        chans = [
            xmltv.OutputChannel("a", "Nova"),
            xmltv.OutputChannel("b", "Nova"),
            xmltv.OutputChannel("z", "Nova (a)"),
        ]
        self.assert_unique_lookups(chans, self.names(chans))

    def test_javascript_whitespace_and_long_titles_count_as_collisions(self):
        chans = [
            xmltv.OutputChannel("a", "News﻿HD"),
            xmltv.OutputChannel("b", "News HD"),
            xmltv.OutputChannel("c", "x" * 256),
            xmltv.OutputChannel("d", "x" * 256 + "X"),
        ]
        shown = self.names(chans)
        self.assertNotEqual(xmltv.name_key(shown[0]), xmltv.name_key(shown[1]))
        self.assertEqual(xmltv.lookup_key(chans[2].name), xmltv.lookup_key(chans[3].name))
        self.assertTrue(shown[2] != shown[3] or len(xmltv.name_key(shown[3])) > 256)
