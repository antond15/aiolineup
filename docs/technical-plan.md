# AIOLineup — Technical Plan

## Overview

A self-hosted filter between public IPTV sources (iptv-org M3U playlists, XMLTV guides such as
iptv-epg.org) and AIOStreams' built-in M3U addon. One admin whitelists channels; the service serves a filtered
`playlist.m3u` and `epg.xml` at stable, token-protected URLs. Constraints that shape everything:
it runs in a 256 MiB container (low memory is a goal), upstream guides reach ~487 MiB of XML, AIOStreams reads at most a 16 MB playlist
(10 000 entries) and a 128 MB guide (200 000 programmes), merges streams into one channel only
when `tvg-id` and title both match, and matches the guide by lowercase `tvg-id`, then by name. Metadata only: no stream proxying, no DRM/geo circumvention.

## Architecture

One Python 3.13 process, standard library only, in one container:

```
web.py (http.server, ≤32 connections) ── queues jobs ──> engine.py worker thread (one)
   │ serves static feed files                               │ fetch.py → m3u.py / xmltv.py
   └── /data: SQLite (db.py), cached guides, feeds  <───────┘ atomic os.replace
```

A timer queues refreshes every `REFRESH_SECONDS` (6 h). Conditional requests (ETag /
Last-Modified) make an unchanged source cost one request. Selection changes rebuild the guide
from the cache after a 15 s debounce.

## Repository layout

See the Development section of the [README](../README.md) (layout block).

## Security

Single admin with a signed session cookie (HttpOnly, SameSite=Strict, 30 days), login rate limit
(10 failures / 15 min), CSRF blocked by JSON body + `X-Requested-With` + same-origin. Feeds need
only the path token (constant-time compare, masked in logs). SSRF: http/https only, public
address check on every hop (embedded IPv4 checked for NAT64 and similar forms), connection
pinned to the checked addresses, ≤5 redirects, size limits during
download and decompression. Expat rejects external entities, DTDs and expansion bombs. Strict
CSP, upstream text via `textContent` only. Secrets from env or `/data` only.

## Data model

SQLite tables: `sources`, `streams`, `selections`, `stream_prefs`, `channel_edits`,
`epg_mappings`, `epg_channels`, `epg_schedule`, `category_overrides`, `meta_languages`, `kv`.
Schema version in `PRAGMA user_version` (currently 7),
migrated in `db.py` on start; every start queues a rebuild of both feeds from cached data on
the worker (a missing file is built before the server starts), since a release usually changes the
output rules. `epg_schedule` holds every guide channel's programmes from scan
time to 48 hours later (title, start, stop), so My channels can show what is on now; a scan
streams them to a temp file and they replace the source's rows in the same transaction as
`epg_channels`, before the cached files are swapped. An unchanged guide (HTTP 304) is
re-scanned from its cached copy once its schedule is 12 hours old. Channel identity: `tvg-id` case-insensitive, else the cleaned exact name. A
stream is a duplicate only when URL and directives match (`#EXTGRP` is a category, not a
directive, and is ignored).

Output `group-title`: the playlist feed carries up to 8 categories per channel as
`group-title="A;B;C"`, the same on every stream of it; AIOStreams turns each `;`-separated part
into a group row and keeps the first 8. The shown list is the admin's own list (schema 7:
`category_overrides(channel_key, position, category)`, 1 to 8 items in the admin's order, set
with the chips editor in the channel panel; each item stored as typed after a
case-insensitive match to another channel's category, so a channel can re-case its own; no
`;`, `|`, `"` or control characters; repeats ignoring case refused), which replaces the source
categories completely, or else every source category (`m3u.categories` order: split on `;` and
`|`, Undefined/Other dropped, source priority). The feed sends the shown list without
case-insensitive repeats (first spelling kept), cut to 8. With no category, no `group-title`
is written. Any upstream `group-title` (any case) and `#EXTGRP` line (any spelling) is
dropped. Editing an unselected channel does not rebuild the feed. The v7 upgrade turned each
saved single category into a one-item list, so those channels' feeds did not change. In the
app the shown list is what the tags, Category filter and facets use; hidden source categories
never match.

Channel lineup (schema 6): every selection has a `number` (`NOT NULL UNIQUE`, at least 1; the
API takes 1 to 9999). A new selection gets the highest number + 1 (past 9999 the lowest free
one), a bulk select appends in name order, and deselecting forgets the number and the stream
choices. A number in use is refused with the holder's name; nothing renumbers, and a missing
channel keeps its number (the feed skips it, so gaps are normal). The v6 upgrade numbered
existing selections 1..N by the feed's old order (folded catalogue name, then key).
`stream_prefs(channel_key, signature, position, enabled)` holds a selected channel's stream
order and on/off, keyed by `m3u.signature_key` (URL plus request directives, the dedup
identity); streams without a row follow the ranked ones in source order, on. A save that leaves
no stream of an enabled playlist on is refused; if the on ones later vanish upstream, the
channel sends no stream (and is not counted in the playlist stats) until one returns. A save
keeps the rows of streams it cannot list (a disabled playlist, gone upstream) after the listed
ones, so an off stream stays off when it comes back; null forgets them too.
`channel_edits(channel_key, name, logo)` holds the admin's own name (1 to 200 characters) and
logo URL (http/https, at most 2048 characters, no `"`); neither holds a control character or one
XML 1.0 forbids (U+FFFE, U+FFFF, surrogates), which would make the whole guide unreadable. NULL
means the source value; it is kept on deselect. `Channel.name` stays the source
name and `feed_name`/`feed_logo` apply the edit, so exported ids and guide matching never
change with a rename. API: `PUT /api/channel/number`, `PUT /api/channel/streams`
(`[{sig, on}]` in feed order, or null), `PUT /api/channel/edit` (`name`, `logo`; `""` or null
resets); `GET /api/channel` lists `feed_streams` with their `sig` and `url_repeat` (an earlier
on stream has the same URL, so AIOStreams ignores this one), and the own name and logo also for
a selected channel missing upstream; `GET /api/channels` takes
`sort=number`. A number or stream change rebuilds the playlist; a name or logo change on a
selected channel also queues the guide (15 s debounce).

## Integrations

- M3U playlists added by the user (a new install starts with no sources)
  and iptv-org `feeds.json` for language metadata (can be turned off).
- XMLTV guides added by the user, plain or gzip.
- AIOStreams' M3U addon consumes the feeds (playlist cached `BUILTIN_M3U_LINEUP_TTL`, guide
  `BUILTIN_M3U_GUIDE_TTL`). Its behaviour is checked against its source (commit `89f03f6`).
  Playlist: every stream of a channel gets the channel's name (the own name if set) as title and
  the shared `tvg-id` (upstream titles and `tvg-name` are dropped), because AIOStreams keys a
  channel on both. Channels are listed by number and every stream carries `tvg-chno="<number>"`,
  which AIOStreams reads as the channel number (`playlist.ts`: `tvg-chno`, else `ch-number`);
  upstream `tvg-chno` and `ch-number` are dropped. An own logo replaces `tvg-logo` on every
  stream and is the guide icon. AIOStreams keeps a channel's streams in playlist order, one per
  URL (the first; `lineup.ts` compares the URL alone, so the same URL with other headers is
  dropped), and offers the first 8 distinct URLs.
  Exported ids: the id is `tvg_id` or the name, and `engine.assign_export_ids` makes it unique the
  way AIOStreams compares ids (cut to 256 UTF-16 units, lowercase; an `@sd|hd|fhd|uhd|4k` tail
  is a fallback to the id without it). Walking selections by `added_at`, the first channel keeps
  its id and a clashing later one gets `~2`, `~3`, ...; an id over 256 units becomes 240 units,
  `~` and 8 hex of its sha1. Computed at each build, never stored; playlist and guide use
  the same result, and guide matching still uses the plain id.
  Guide: a selected channel whose name collides with another's under AIOStreams' name key
  (lowercase, whitespace and `_` as one space) gets `display-name` `<name> (<id>)`, so a channel
  without programmes never takes a namesake's through the name fallback. The UI warns when a feed
  passes an AIOStreams limit.
- A failed fetch keeps the last good data and is shown per source.

## Background jobs

One worker thread runs refreshes, parsing and builds sequentially; duplicate requests merge.

## Features

See [features](features.md).

## UI

Vanilla JS single page in `aiolineup/static/`, five screens (Overview, My channels, Add channels,
Sources, Settings), no build step. Details live in `DESIGN.md`.

## Backups

Manual only. Settings → Backup exports the admin's own data (sources with their ids, selections
with `added_at` and `number`, own categories, names and logos, stream prefs, guide mappings,
settings) as JSON with its own format version (`aiolineup-backup`, version 1; a newer version,
another format or an unknown key is refused). Not a SQLite copy, and never the cache, stats,
feed token, password or session secret. Source URLs are exported exactly.

- `aiolineup/backup.py`: `export` (one read transaction, rows streamed to an anonymous temp
  file, sent with `Content-Length`), `check` (every row validated with the rules the API uses,
  which live in `aiolineup/rules.py`; mappings must name a guide in the file; numbers unique),
  `apply` (one transaction).
- Routes: `GET /api/backup`; `POST /api/backup/import` (the file as the JSON body, 12 MiB cap;
  `?check=1` validates and returns the summary only).
- Apply replaces everything and keeps source ids, so `epg_mappings.source_id` stays right. It
  runs only while the worker's `Engine.job_lock` is free (else 409); the worker holds that lock
  around each job and drops a job it took before an import (`_generation`). After the commit the
  cache files of every source id are deleted (a new id can equal an old one), the pending set is
  replaced by a refresh of all enabled sources plus the playlist and guide builds, so the feeds
  rebuild after the downloads in one job.
- Logs hold counts only. Peak memory is measured in `docs/deployment.md` → Backups.

See `docs/deployment.md` → Backups for how to use it.

## Testing

`unittest` suites per module (parsing, identity, dedup, persistence, auth, failure recovery, XML
safety, a memory ceiling test), no network. CI runs the syntax check, the tests and the Docker
build on every branch except `main`. No browser e2e. Ruff (format and lint, pinned, run through `uvx`) also runs in
CI's Check job.

## Roadmap

The service is complete for its scope: sources, channel selection, guide matching, the two feeds,
backups and the admin UI are built and in use.

## Open questions

None.
