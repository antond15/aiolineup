# AIOLineup

Pick your channels from public IPTV playlists and guides and serve one clean M3U playlist and
XMLTV guide. AIOLineup is a small self-hosted service: you add source URLs, and it turns big
playlists (M3U) and programme guides (XMLTV) into one short playlist and one matching guide.

```text
M3U playlists ──┐                      ┌── playlist.m3u ──┐
                ├─── AIOLineup (UI) ───┤                  ├── your player / addon
XMLTV guides ───┘                      └── epg.xml ───────┘
```

You add source URLs, tick the channels you want in a web UI, and point your player at two
stable feed URLs. The feeds are rebuilt in the background whenever sources refresh or your
selection changes, so the URLs never change.

AIOLineup handles playlist and guide **metadata only**. Video streams are never proxied,
downloaded or transcoded, and nothing tries to get around DRM, subscriptions or geo-blocking.

## Features

* **Any standard source.** M3U playlists and XMLTV guides (plain or `.gz`) over http/https,
  refreshed on a schedule. Unchanged sources cost a single request, and a failed refresh
  keeps the last good data.
* **Whitelist, not blacklist.** Nothing is selected until you select it. Search and filter by
  name, id, source, category, country, language, selection and guide coverage, and select or
  deselect everything a filter matches. Selections survive restarts and upstream changes.
* **Alternatives kept together.** The same channel from several sources or URLs becomes one
  channel with several streams. Similar names are never merged by guesswork.
* **Guide matching.** Each channel gets its programmes from one guide, matched by id, with
  manual mappings and suggestions for the rest. The My channels page explains why a channel
  has no guide data.
* **Small output.** The guide keeps only your channels and a configurable time window, and is
  served gzipped to clients that accept it.
* **Light and self-contained.** One container, Python standard library only, no database
  server, no build step.

The detailed behaviour (identity rules, matching order, limits) is in
[docs/features.md](docs/features.md).

## Quick start

You need Docker. The only required setting is an admin password of at least 8 characters.
The commands below generate a random one and print it; keep it, you sign in with it.

```sh
git clone https://github.com/antond15/aiolineup.git
cd aiolineup
docker build -t aiolineup .
ADMIN_PASSWORD="$(openssl rand -base64 18)"; echo "Admin password: $ADMIN_PASSWORD"
docker run -d --name aiolineup --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v aiolineup_data:/data \
  -e ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  --memory 256m \
  aiolineup
```

The port is bound to `127.0.0.1`, so only this machine can reach it. To serve it to other
devices, put a reverse proxy with HTTPS in front (see [Deployment](#deployment)) rather than
publishing the port to the network.

Open http://localhost:8080 and sign in as `admin`. Then:

1. **Sources:** add one or more M3U playlists, and optionally XMLTV guides. A new install
   starts with no sources (see [Finding sources](#finding-sources)).
2. **Add channels:** search the playlists and select the channels you want.
3. **My channels:** put them in order, check guide coverage (filter "Needs guide") and map any
   channel that was not matched automatically, in its panel.
4. **Overview:** copy the feed URLs into your player.

### With Docker Compose

`docker-compose.yml` defines the same container with a named volume, a health check and a
256 MiB memory limit. Its variable names (`SERVICE_PASSWORD_ADMIN`, `SERVICE_HEX_64_FEEDTOKEN`,
`SERVICE_URL_AIOLINEUP_8080`) follow a convention some hosting panels use to generate secrets.
Elsewhere, set them in a `.env` file:

```sh
cp .env.example .env     # set SERVICE_PASSWORD_ADMIN (the app refuses to start while it is empty)
docker compose up -d --build
```

The compose file publishes no port. Add `ports: ["127.0.0.1:8080:8080"]` to the service, or
attach it to your reverse proxy's network.

### Without Docker

You need Python 3.13. There is nothing to install.

```sh
ADMIN_PASSWORD="$(openssl rand -base64 18)"; echo "Admin password: $ADMIN_PASSWORD"
ADMIN_PASSWORD="$ADMIN_PASSWORD" HOST=127.0.0.1 DATA_DIR=./data python3 -m aiolineup
```

## Deployment

* **Put it behind HTTPS.** Run it behind any reverse proxy (Caddy, nginx, Traefik, a hosting
  panel) that terminates TLS and forwards to port 8080. Session cookies get the `Secure` flag
  automatically when the proxy sends `X-Forwarded-Proto: https`.
* **Set `PUBLIC_URL`** to the address your player will use, e.g. `https://iptv.example.com`.
  Without it, the URLs shown in the UI come from the request headers, and the optional guide
  link inside the playlist is left out.
* **Keep the `/data` volume.** It holds the SQLite database (sources, selections, mappings,
  settings), the generated feed token, cached guides and the built feeds. Only the database
  can't be rebuilt by a refresh, so back it up if your selections matter to you.
* **Keep the feed token stable.** If you don't set `FEED_TOKEN`, one is generated on first
  start and stored in `/data/feed_token`. Losing it changes your feed URLs.
* **Updating:** pull, rebuild and restart. Schema migrations run on startup. Downgrades are
  not supported.

The container runs as a non-root user (uid 10001), listens on port 8080 and answers `GET
/health` for health checks. Logs go to stdout.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_PASSWORD` | required | Web UI password, at least 8 characters. Changing it signs out all sessions. |
| `ADMIN_USER` | `admin` | Web UI user name. |
| `FEED_TOKEN` | generated | Secret part of the feed URLs: at least 16 characters of `[A-Za-z0-9_-]`, e.g. from `openssl rand -hex 32`. |
| `PUBLIC_URL` | from request headers | Public base URL, used for the feed URLs shown in the UI and the playlist's guide link. |
| `REFRESH_SECONDS` | `21600` | Automatic refresh interval (6 h). `0` turns it off. |
| `REQUEST_TIMEOUT` | `60` | Socket timeout in seconds for upstream requests. A whole download is capped at 15 min. |
| `M3U_MAX_MB` | `20` | Size limit for a playlist, after decompression. |
| `EPG_MAX_DOWNLOAD_MB` | `200` | Size limit for a guide download (compressed). |
| `EPG_MAX_DECOMPRESSED_MB` | `1024` | Limit on decompressed guide XML (guards against gzip bombs). |
| `ALLOW_PRIVATE_SOURCES` | `false` | Allow sources on LAN, loopback or link-local addresses. |
| `SECURE_COOKIES` | auto | Force the `Secure` cookie flag on (`true`) or off (`false`). |
| `DATA_DIR` | `/data` | Where state is stored. |
| `HOST` | `0.0.0.0` | Address to listen on. Use `127.0.0.1` to keep it local. |
| `PORT` | `8080` | Listening port. |
| `LOG_LEVEL` | `INFO` | Python log level. |

Guide window (days ahead, hours back) and language lookup are set in the UI under **Settings**.

## Using the feeds

The **Overview** page shows three URLs:

```text
https://iptv.example.com/feed/<token>/playlist.m3u
https://iptv.example.com/feed/<token>/epg.xml
https://iptv.example.com/feed/<token>/epg.xml.gz
```

Add the playlist as an M3U source and the guide as an XMLTV source in any player or addon that
reads them, for example the built-in M3U addon of
[AIOStreams](https://github.com/Viren070/AIOStreams) (paste the playlist URL, and put
`epg.xml.gz` in its Guides field), TiviMate, Kodi's IPTV Simple Client or VLC. The feeds need only the token, with no login, so treat the URLs like
passwords.

* Every channel in the playlist has a `tvg-id` that matches its guide entry, so players match
  them reliably. Country and language are added as `tvg-country` and `tvg-language` where known.
  When two selected channels would read as one in AIOStreams (same id ignoring case, or one is
  the other plus `@HD`), the one selected later gets `~2` after its id.
* If your player only takes a playlist, turn on the guide link in **Settings**. It adds an
  `x-tvg-url` header pointing at `epg.xml` (needs `PUBLIC_URL`).
* Players cache sources too, so a change can take a few minutes to appear.
* Every stream of a channel carries the same title (the channel's name) and `tvg-id`, and
  upstream `tvg-name` is dropped, so AIOStreams shows alternatives as one channel and matches
  the guide by id. Channels whose names collide get their id appended to the guide's display
  name. AIOStreams keeps at most 8 addresses per channel; other players use them all.
* AIOStreams reads a playlist up to 16 MB with 10 000 entries, and a guide up to 128 MB
  decompressed with 200 000 programmes. The UI warns you when a feed passes one. A few dozen
  channels with a week of data come to a few MiB.

## Finding sources

* **Playlists:** [iptv-org/iptv](https://github.com/iptv-org/iptv) publishes playlists of
  publicly available channels by country, category and language. Any other standard M3U URL works too.
* **Guides:** use any XMLTV guide whose channel ids match the ids in your playlists. Guides
  built for iptv-org ids match those playlists best. Channels that still don't match can be
  mapped by hand in a channel's panel on My channels.

When language lookup is on, the app also reads iptv-org's public channel database to fill in
missing languages. You can turn this off in **Settings**.

## Security

* The admin UI uses a signed, HttpOnly, `SameSite=Strict` session cookie, rate-limited logins
  and CSRF checks on every change. There is a single admin account.
* Feed tokens are compared in constant time and masked in logs.
* Upstream fetches are limited to public http/https addresses, checked on every redirect and
  pinned against DNS rebinding, with size and time limits.
* The XML parser refuses external entities and entity-expansion bombs. The UI runs under a
  strict Content Security Policy.

Details are in [docs/features.md](docs/features.md#security).

## Resource use

Guides are streamed and filtered as they are read, never loaded whole. Filtering a guide of
several hundred MiB takes a few tens of MiB of memory, a running instance idles at around
40 MiB, and it uses no CPU between refreshes. Small servers handle it fine.
`scripts/measure_memory.py` measures peak memory for your own guides.

## Limitations

* One admin user, no user management.
* The login rate limit is shared by all clients: after repeated failed logins, even the correct
  password is refused until the window passes.
* Guide matching is by id, plus manual mappings. Name similarity is only ever a suggestion.
* Automatic language lookup only covers iptv-org-style ids. Other playlists need
  `tvg-language`.
* Raw guides are cached on disk so they can be re-filtered without downloading them again.
  Large guides take some disk space.
* Feeds refresh on a timer and are not pushed. Players add their own caching on top.

## Development

```sh
ADMIN_PASSWORD=devpassword HOST=127.0.0.1 DATA_DIR=./data python3 -m aiolineup   # run locally
python3 -m unittest discover -s tests -t .                                  # tests, no network
uvx ruff@0.17.6 format . && uvx ruff@0.17.6 check .                         # format and lint
```

```text
aiolineup/   config, db (SQLite), fetch (hardened HTTP), m3u, xmltv (streaming),
              engine (background worker), backup (export/import), rules (input checks
              shared by the API and the backup import), web (server, auth, API, feeds),
              static/ (UI)
tests/        unittest suites
scripts/      measure_memory.py
docs/         features, technical plan, deployment
```

## License

[MIT](LICENSE). The project is provided as is, without support.
