import gzip
import os
import tempfile
from pathlib import Path

from aiolineup import config
from aiolineup.db import Database
from aiolineup.engine import Engine

PLAYLIST_A = """#EXTM3U
#EXTINF:-1 tvg-id="RedBullTV.at@SD" tvg-logo="https://example.org/rb.png" group-title="Sports",Red Bull TV (1080p)
https://cdn.example.org/redbull/1080.m3u8
#EXTINF:-1 tvg-id="RedBullTV.at@SD" group-title="Sports",Red Bull TV (720p)
#EXTVLCOPT:http-user-agent=Mozilla/5.0
https://cdn.example.org/redbull/720.m3u8
#EXTINF:-1 tvg-id="TopGear.uk@SD" group-title="Entertainment;Auto",Top Gear
https://cdn.example.org/topgear.m3u8
#EXTINF:-1 group-title="Sports",Speed Sport 1 [Geo-blocked]
https://cdn.example.org/speed1.m3u8
"""

# Same Red Bull 1080 stream again (duplicate), plus an alternative and a new channel.
PLAYLIST_B = """#EXTM3U
#EXTINF:-1 tvg-id="redbulltv.at@SD" group-title="Outdoor",Red Bull TV
https://cdn.example.org/redbull/1080.m3u8
#EXTINF:-1 tvg-id="RedBullTV.at@SD" group-title="Outdoor",Red Bull TV backup
https://backup.example.org/redbull.m3u8
#EXTINF:-1 group-title="Sports",Speed Sport 1 (720p)
https://mirror.example.org/speed1.m3u8
#EXTINF:-1 tvg-id="NBATV.us@SD" group-title="Sports",NBA TV
https://cdn.example.org/nba.m3u8
"""


def guide(channels: dict[str, list[tuple[str, str, str]]], extra: str = "") -> str:
    """channels: id -> [(start, stop, title)]"""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n<tv>']
    for cid in channels:
        parts.append(f'<channel id="{cid}"><display-name>{cid} name</display-name></channel>')
    for cid, progs in channels.items():
        for start, stop, title in progs:
            parts.append(
                f'<programme start="{start}" stop="{stop}" channel="{cid}"><title lang="en">{title}</title>'
                f"<desc>About {title} &amp; more</desc></programme>"
            )
    parts.append(extra)
    parts.append("</tv>")
    return "\n".join(parts)


def write_gz(path: Path, text: str) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)
    return path


class TempEnv:
    """Fresh data dir + config + db + engine."""

    def __init__(self, **env):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = {
            "DATA_DIR": str(self.dir),
            "ADMIN_PASSWORD": "correct-horse",
            "FEED_TOKEN": "feedtoken_0123456789",
            "REFRESH_SECONDS": "0",
            **env,
        }
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.cfg = config.load()
        self.db = Database(self.cfg.db_path)
        self.engine = Engine(self.cfg, self.db)

    def reopen(self):
        self.db.close()
        self.db = Database(self.cfg.db_path)
        self.engine = Engine(self.cfg, self.db)
        return self.engine

    def close(self):
        self.db.close()
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()
