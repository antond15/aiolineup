# Deployment

One Docker Compose service (`aiolineup`) publishes no host port: a reverse proxy or hosting panel
routes HTTPS traffic to container port 8080. The container runs as uid 10001 with
`mem_limit: 256m` and `restart: unless-stopped`. Memory use matters, because small hosts often run
other services alongside it.

## First-time setup

[Coolify](https://coolify.io) is one example host. Any other Docker host works too (see the
README's Quick start).

1. In Coolify, create a resource from the repository with the **Docker Compose** build pack and
   `docker-compose.yml`.
2. Set the domain on the `aiolineup` service to `https://aiolineup.example.com`.
3. Deploy. Coolify generates `SERVICE_PASSWORD_ADMIN` (admin password) and
   `SERVICE_HEX_64_FEEDTOKEN` (feed URL secret); both persist across redeploys. The proxy routes
   to port 8080 through `SERVICE_URL_AIOLINEUP_8080`, and its plain sibling
   `SERVICE_URL_AIOLINEUP` (without the port) is passed to the app as `PUBLIC_URL` so the UI shows
   the public feed URLs.
4. Sign in; the Overview page shows the feed URLs to add to your player (see README, "Using the
   feeds"). Other variables are listed in the README's configuration table.

## Updating

Pull the new version (or point the host at the branch), rebuild the image and redeploy. Schema
migrations run on startup; downgrades are not supported (see Migrations). Afterwards check
`GET /health` and the Overview page.

## Migrations

The SQLite schema is versioned with `PRAGMA user_version`; `db.py` migrates on startup. There is
no separate migration step, and a downgrade is not supported. Each upgrade runs in one
transaction, so a failed one leaves the file as it was and the container stops. Schema 6 (channel
numbers) makes the number required: an older image on a v6 file cannot select channels, so a
rollback past it needs a copy of `/data` taken before the upgrade.

## Healthchecks

`GET /health` from inside the container every 30 s (5 s timeout, 30 s start period, 3 retries),
defined in both the `Dockerfile` and `docker-compose.yml`.

## Logs

stdout/stderr, viewed in your host's log view. Level from `LOG_LEVEL` (default `INFO`). Feed tokens
are masked as `/feed/***/` and query strings are never logged. Your reverse proxy or hosting
panel may log the full request URL, including the token, so check its access-log settings.

## Backups

All state is in the data volume (`/data`): the SQLite database (sources, selections, EPG
mappings, settings), `feed_token` if generated, cached raw guides and the generated feeds. Only
the database holds anything that cannot be rebuilt by a refresh.

Settings → Backup keeps the admin's own work as one JSON file (manual backups only):

- **Export:** "Export backup" downloads `aiolineup-backup-YYYY-MM-DD.json`. It is read in one
  transaction, so it is consistent while a refresh runs.
- **Import:** "Import backup…" picks such a file. The server checks it first and writes
  nothing; the panel shows the date, the counts (sources, channels, guide mappings, settings)
  and asks to confirm. "Replace with backup" then replaces all current sources, channels and
  settings in one transaction (a file with any bad row is refused whole, naming the row), clears
  the cached playlists, guides and stats, and queues a refresh of every enabled source, then the
  feeds are rebuilt. It is refused with "A refresh is running" while the worker has a job; try
  again when it finishes. The file may be up to 12 MiB (a real one is tens of KiB). An export over
  that is refused with a message instead of downloaded, because no import would take it.
- **What the file holds:** sources (id, kind, name, URL, enabled, priority, created time),
  selected channels (with their numbers and the time they were added), own categories, names
  and logos, saved stream order and on/off (these name stream URLs), guide mappings and
  settings. It holds no cache, stats, feed token, admin password or session secret.
- **Logins in source URLs.** Source URLs are exported exactly, so a login or key in a source's
  query string is in the file. Keep the file private, like the feed URLs.
- **Keep the feed URLs.** The feed token is not in the file. Set `FEED_TOKEN` (on Coolify,
  `SERVICE_HEX_64_FEEDTOKEN`) to the old value before the first start of a new deployment, or the
  feed URLs change and must be entered again in AIOStreams. The channel ids AIOStreams sees
  follow the order channels were added, which the file keeps (`added_at`), so they survive.
- **Memory:** `scripts/measure_memory.py <backup.json>` runs the import and an export into a
  throwaway database. The densest file there can be (11.9 MiB, 213 000 one-word category rows)
  peaks at 115 MiB RSS (interpreter baseline 26 MiB), 1.9 s; a typical large one (14.9 MiB,
  10 000 channels with categories, own names, mappings and stream choices) peaked at 88 MiB.
  A 17 MiB dense file reached 155 MiB, which is why the cap is 12 MiB: it keeps the worst case
  under the 128 MiB bar and the container's 256 MiB limit. Rows are held as tuples and each
  section of the parsed file is freed once it is checked.

The data volume itself is not backed up by the service. If it is lost: deploy a fresh instance
with the same environment variables, sign in, and import the last exported file; the refresh that
follows fetches the sources again.

## Trying the stack locally

```sh
cp .env.example .env    # set SERVICE_PASSWORD_ADMIN and SERVICE_HEX_64_FEEDTOKEN
docker compose up -d --build    # add ports: ["8080:8080"] to reach it from the host
```
