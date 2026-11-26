"""Entry point: python -m aiolineup"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from . import __version__, config
from .db import Database
from .engine import Engine
from .web import serve


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    cfg = config.load()
    db = Database(cfg.db_path)
    engine = Engine(cfg, db)
    engine.start()
    server = serve(cfg, engine)
    logging.getLogger("aiolineup").info(
        "aiolineup %s listening on %s:%d", __version__, cfg.host, cfg.port
    )

    def shutdown(*_):
        engine.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever(poll_interval=1.0)


if __name__ == "__main__":
    main()
