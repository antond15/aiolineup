#!/usr/bin/env python3
"""Measure time and peak RSS of one refresh path, in a fresh process.

    python scripts/measure_memory.py path/to/guide.xml.gz [comma,separated,ids]
    python scripts/measure_memory.py path/to/playlist.m3u[8][.gz]
    python scripts/measure_memory.py path/to/aiolineup-backup.json

A backup (.json) runs the import the server runs (parse, check, apply) into a throwaway
database, then an export. An XMLTV guide runs the scan the server runs; a playlist (.m3u, .m3u8, optionally .gz) runs
the whole M3U refresh (download from a local copy, parse, insert) into a throwaway database.
"""

import os
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiolineup import xmltv  # noqa: E402


def rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def measure_playlist(playlist: Path) -> None:
    from aiolineup import config, fetch
    from aiolineup.db import Database
    from aiolineup.engine import Engine

    with tempfile.TemporaryDirectory() as tmp:
        os.environ.update(DATA_DIR=tmp, ADMIN_PASSWORD="measure-only", FEED_TOKEN="m" * 20)
        cfg = config.load()
        db = Database(cfg.db_path)
        engine = Engine(cfg, db)
        with db.conn() as c:
            c.execute("DELETE FROM sources")
            c.execute(
                "INSERT INTO sources (id, kind, name, url, created_at)"
                " VALUES (1, 'm3u', 'measure', 'https://example.invalid/p.m3u', 0)"
            )

        def local_copy(url, dest, **kwargs):
            shutil.copyfile(playlist, dest)
            return fetch.FetchResult(url, None, None, playlist.stat().st_size)

        fetch.download_to_file = local_copy
        cfg_limit = max(cfg.m3u_max_bytes, 10 * playlist.stat().st_size)
        object.__setattr__(cfg, "m3u_max_bytes", cfg_limit)
        before = rss_mib()
        started = time.monotonic()
        changed = engine.refresh_m3u(db.one("SELECT * FROM sources WHERE id = 1"))
        elapsed = time.monotonic() - started
        src = db.one("SELECT * FROM sources WHERE id = 1")
        print(f"playlist:         {playlist} ({playlist.stat().st_size / 2**20:.1f} MiB on disk)")
        print(
            f"stored:           {src['item_count']} entries (stored: {changed}, error: {src['last_error']})"
        )
        print(f"time:             {elapsed:.1f} s")
        print(f"peak RSS:         {rss_mib():.1f} MiB (interpreter baseline {before:.1f} MiB)")
        db.close()


def measure_backup(path: Path) -> None:
    import json

    from aiolineup import backup, config
    from aiolineup.db import Database
    from aiolineup.engine import Engine

    with tempfile.TemporaryDirectory() as tmp:
        os.environ.update(DATA_DIR=tmp, ADMIN_PASSWORD="measure-only", FEED_TOKEN="m" * 20)
        cfg = config.load()
        db = Database(cfg.db_path)
        engine = Engine(cfg, db)
        before = rss_mib()
        started = time.monotonic()
        with open(path, "rb") as fh:
            doc = json.loads(fh.read())  # what the server does with the request body
        parsed = backup.check(doc)
        del doc
        refreshing = backup.apply(engine, parsed)
        elapsed = time.monotonic() - started
        print(f"backup:           {path} ({path.stat().st_size / 2**20:.1f} MiB on disk)")
        print(f"imported:         {parsed.summary()['counts']}, {refreshing} sources to refresh")
        print(f"time:             {elapsed:.1f} s (check + apply)")
        print(f"peak RSS:         {rss_mib():.1f} MiB (interpreter baseline {before:.1f} MiB)")
        with tempfile.TemporaryFile() as out:
            backup.export(db, out)
            print(f"export:           {out.tell() / 2**20:.1f} MiB written")
            print(f"peak RSS:         {rss_mib():.1f} MiB after export")
        db.close()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    guide = Path(sys.argv[1])
    if guide.suffix.lower() == ".json":
        return measure_backup(guide)
    if guide.name.lower().removesuffix(".gz").endswith((".m3u", ".m3u8")):
        return measure_playlist(guide)
    wanted = {
        xmltv.norm_id(i) for i in (sys.argv[2] if len(sys.argv) > 2 else "").split(",") if i.strip()
    }
    before = rss_mib()
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        sched = Path(tmp) / "schedule.tsv"
        result = xmltv.scan(
            guide, wanted, Path(tmp) / "fragment.xml", 4 * 1024**3, schedule_path=sched
        )
        sched_lines = sum(1 for _ in open(sched, encoding="utf-8"))
        sched_bytes = sched.stat().st_size
    elapsed = time.monotonic() - started
    print(f"guide:            {guide} ({guide.stat().st_size / 2**20:.1f} MiB on disk)")
    print(f"decompressed:     {result.decompressed_bytes / 2**20:.1f} MiB")
    print(f"channels indexed: {len(result.index)}")
    print(
        f"programmes:       {sum(e.programmes for e in result.index.values())} total, {result.kept} kept"
    )
    print(f"schedule (48 h):  {sched_lines} programmes, {sched_bytes / 2**20:.1f} MiB as text")
    print(f"time:             {elapsed:.1f} s")
    print(f"peak RSS:         {rss_mib():.1f} MiB (interpreter baseline {before:.1f} MiB)")


if __name__ == "__main__":
    main()
