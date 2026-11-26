import unittest

from aiolineup import m3u


class ParseTests(unittest.TestCase):
    def test_attributes_and_title(self):
        r = m3u.parse_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="A.us@SD" tvg-name=\'Alpha, The\' group-title="Sports;News" '
            'tvg-logo=https://x/y.png catchup="default",Alpha, The Channel\nhttp://s/1.m3u8\n'
        )
        self.assertTrue(r.had_header)
        self.assertEqual(len(r.entries), 1)
        e = r.entries[0]
        self.assertEqual(e.title, "Alpha, The Channel")
        self.assertEqual(e.tvg_id, "A.us@SD")
        self.assertEqual(e.attr("tvg-name"), "Alpha, The")
        self.assertEqual(e.attr("tvg-logo"), "https://x/y.png")
        self.assertEqual(e.attr("catchup"), "default")
        self.assertEqual(m3u.categories(e.group), ["Sports", "News"])

    def test_bom_crlf_directives_and_extgrp(self):
        text = "﻿#EXTM3U\r\n#EXTINF:0,Bare\r\n#EXTGRP:Movies\r\n#EXTVLCOPT:http-referrer=https://r/\r\n# plain comment\r\nhttp://s/2\r\n"
        e = m3u.parse_text(text).entries[0]
        self.assertEqual(e.duration, "0")
        self.assertEqual(e.group, "Movies")
        self.assertEqual(e.directives, ["#EXTGRP:Movies", "#EXTVLCOPT:http-referrer=https://r/"])

    def test_malformed_entries_are_skipped(self):
        text = "#EXTM3U\nhttp://orphan/url\n#EXTINF:-1,No url\n#EXTINF:-1,Good\nhttp://ok\n#EXTINF:-1,\nhttp://nameless\n#EXTINF:-1,Dangling"
        r = m3u.parse_text(text)
        self.assertEqual([e.title for e in r.entries], ["Good"])
        self.assertEqual(r.skipped, 4)

    def test_kodi_style_url_with_spaces_kept(self):
        r = m3u.parse_text("#EXTINF:-1,X\nhttp://h/p.m3u8|User-Agent=Mozilla/5.0 (X11)\n")
        self.assertEqual(r.entries[0].url, "http://h/p.m3u8|User-Agent=Mozilla/5.0 (X11)")

    def test_attribute_names_are_case_insensitive(self):
        r = m3u.parse_text('#EXTINF:-1 TVG-ID="Up.us@SD" TVG-NAME="Upper",\nhttp://s/1\n')
        self.assertEqual(r.skipped, 0)
        self.assertEqual(r.entries[0].title, "Upper")
        self.assertEqual(r.entries[0].tvg_id, "Up.us@SD")
        r = m3u.parse_text('#EXTINF:-1 TVG-ID="Only.us",\nhttp://s/1\n')  # id alone is enough
        self.assertEqual(len(r.entries), 1)

    def test_streams_lazily_with_stats(self):
        stats = m3u.ParseStats()
        lines = iter(["﻿#EXTM3U\n", "#EXTINF:-1,A\n", "http://a\n", "http://orphan\n"])
        gen = m3u.parse(lines, stats)
        self.assertEqual(next(gen).title, "A")
        self.assertEqual(stats.skipped, 0)  # nothing is read ahead of what was consumed
        self.assertEqual(list(gen), [])
        self.assertEqual((stats.skipped, stats.had_header), (1, True))

    def test_without_header(self):
        r = m3u.parse_text("#EXTINF:-1,X\nhttp://h/p\n")
        self.assertFalse(r.had_header)
        self.assertEqual(len(r.entries), 1)


class IdentityTests(unittest.TestCase):
    def test_clean_name(self):
        self.assertEqual(m3u.clean_name("Red Bull TV (1080p) [Not 24/7]"), "Red Bull TV")
        self.assertEqual(m3u.clean_name("Channel 4 (UK)"), "Channel 4 (UK)")  # not a quality tag
        self.assertEqual(m3u.clean_name("[Geo-blocked]"), "[Geo-blocked]")  # never empty

    def test_keys(self):
        a = m3u.Entry(title="Red Bull TV (720p)", url="u", attrs={"tvg-id": "RedBullTV.at@SD"})
        b = m3u.Entry(title="Something else", url="u", attrs={"tvg-id": "redbulltv.at@sd"})
        self.assertEqual(
            m3u.channel_key(a), m3u.channel_key(b)
        )  # id wins over names, case-insensitive
        feed = m3u.Entry(title="Red Bull TV", url="u", attrs={"tvg-id": "RedBullTV.at@US"})
        self.assertNotEqual(m3u.channel_key(a), m3u.channel_key(feed))  # different feeds stay apart
        n1 = m3u.Entry(title="Speed Sport 1 [Geo-blocked]", url="u")
        n2 = m3u.Entry(title="speed sport 1 (720p)", url="u")
        n3 = m3u.Entry(title="Speed Sport 2", url="u")
        self.assertEqual(m3u.channel_key(n1), m3u.channel_key(n2))
        self.assertNotEqual(m3u.channel_key(n1), m3u.channel_key(n3))  # similar is not equal

    def test_country_and_split(self):
        self.assertEqual(m3u.split_tvg_id("TopGear.uk@SD"), ("TopGear.uk", "SD"))
        self.assertEqual(m3u.country_from_id("TopGear.uk@SD"), "UK")
        self.assertEqual(m3u.country_from_id("Something.pluto"), "")
        self.assertEqual(m3u.country_from_id("NoDots"), "")

    def test_signature(self):
        self.assertEqual(
            m3u.stream_signature(" u ", ["b", "a"]), m3u.stream_signature("u", ["a", "b"])
        )
        self.assertNotEqual(
            m3u.stream_signature("u", []), m3u.stream_signature("u", ["#EXTVLCOPT:x"])
        )


class RenderTests(unittest.TestCase):
    def test_round_trip_is_sanitised(self):
        out = m3u.render_entry(
            'Na"me\n', "http://u\n", {"tvg-id": 'a"b', "x-custom": "1"}, ["#EXTVLCOPT:a=b"]
        )
        self.assertEqual(
            out, '#EXTINF:-1 tvg-id="a\'b" x-custom="1",Na"me\n#EXTVLCOPT:a=b\nhttp://u\n'
        )
        parsed = m3u.parse_text("#EXTM3U\n" + out).entries[0]
        self.assertEqual(parsed.attr("x-custom"), "1")
        self.assertEqual(parsed.url, "http://u")


if __name__ == "__main__":
    unittest.main()
