# Features in detail

The README gives the overview. This page lists the exact rules the service follows.

## Sources

* M3U playlists and XMLTV guides can be added, edited, enabled, disabled, deleted and
  refreshed. Each source shows its status, last success time and last error.
* Refreshes run on a schedule (`REFRESH_SECONDS`, every 6 h by default) and send
  `ETag`/`Last-Modified`, so an unchanged source costs one request.
* A failed refresh never replaces the last good data.
* A new install starts with no sources.

## Channel identity

* With a `tvg-id`, the id is the identity, compared case-insensitively. Feeds of one channel
  with different `@feed` suffixes, such as `France24.fr@English` and `France24.fr@French`, stay
  separate channels, because they are different feeds.
* Without a `tvg-id`, the identity is the exact name after removing quality and status tags
  such as `(1080p)`, `[Geo-blocked]` or `[Not 24/7]`. Similar names are never merged.
* Entries with the same identity from any source become one channel with several streams. A
  stream counts as a duplicate only when its URL *and* its `#EXTVLCOPT`/`#KODIPROP` directives
  match. Every other alternative is kept.

## Whitelist

* Nothing is selected automatically. Selections are stored in SQLite and survive restarts and
  refreshes.
* If a selected channel disappears upstream, it stays selected and is shown as "missing". It
  comes back by itself when it reappears.
* The Add channels page searches by name (the own name and the source name) or `tvg-id`,
  filters by source, category, country, language, selection and guide coverage, can select or
  deselect everything a filter matches, and shows the number of each selected channel.
* The My channels page lists the selected channels by number. A row's number is a field (type,
  Enter, the list re-sorts); a click on the row opens a panel with the number, own name and
  logo, category, streams (order, on/off, a divider after the 8th stream AIOStreams offers)
  and the guide match, plus Remove from My channels.
* Every selected channel has a number from 1 to 9999 that sets its place in the feed. A newly
  selected channel goes to the end (highest number + 1); selecting everything a filter matches
  adds them in name order. Numbers can have gaps, a number already in use is refused ("Number 1
  is used by Red Bull TV."), and nothing is renumbered. Deselecting a channel forgets its
  number and stream choices; selecting it again puts it at the end.
* A selected channel's streams can be put in another order and switched off. At least one
  stream stays on. A stream that appears later upstream goes after the ordered ones, on.
* A channel can get its own name and logo URL; clearing either goes back to the source value.
  Both are kept when the channel is deselected.

## Metadata

* Category comes from `group-title`.
* Country comes from `tvg-country`, or else from the country code in ids like `CNN.us`.
* Language comes from `tvg-language`, or else from the iptv-org channel database
  (`feeds.json`) when **Settings → Look up language metadata** is on.

## Output playlist

* Original attributes and `#EXTVLCOPT`/`#KODIPROP` directives are kept. Upstream URL changes
  appear after the next refresh.
* Every channel carries a `tvg-id` that matches its entry in the generated guide. A channel
  without an upstream id gets a generated one, so players group its alternatives into one
  channel.
* Every stream of a channel has the channel's name (its own name, if set) as its title, and
  upstream `tvg-name` is dropped: AIOStreams merges streams only when `tvg-id` and title both
  match. Renaming never changes the `tvg-id`.
* Channels are listed by number, and every stream carries `tvg-chno` with it, which AIOStreams
  shows as the channel number. Upstream `tvg-chno` and `ch-number` are dropped.
* Streams follow the channel's stream order; switched-off ones are left out. AIOStreams keeps
  one stream per URL (the first, so a later copy with other headers is ignored) and offers the
  first 8 distinct URLs of a channel.
* An own logo replaces `tvg-logo` on every stream of the channel and is its guide icon; an own
  name is also its guide `display-name`.
* `tvg-country` and `tvg-language` are added where known.
* Optionally, the `#EXTM3U` header gets an `x-tvg-url` pointing at the guide (needs
  `PUBLIC_URL`).

## Guide (EPG)

* Guides can be plain XML or `.gz`. They are downloaded to disk and parsed incrementally, and
  every element that isn't needed is discarded as soon as it is read.
* **Automatic matching** tries the exact `tvg-id`, then the id without its `@feed` suffix
  (`CNN.us@East` → `CNN.us`). Ids are compared ignoring case and accents, and leftover
  character references are decoded, so `TéléQuébec.ca` and a double-escaped
  `T&#xE9;l&#xE9;Qu&#xE9;bec.ca` both match `TeleQuebec.ca@SD`.
* **Manual mappings** cover everything else. A channel's panel on My channels searches the
  guides' channel lists by id or display name, ignoring accents.
* **One guide per channel.** Each channel takes its programmes from the highest-priority guide
  that has programmes for it inside the output window. A guide that only declares the channel,
  or has only stale data for it, falls through to the next one. Guides are never merged into
  overlapping schedules.
* **Coverage report.** For each selected channel without guide data, My channels (filter "Needs guide") says why: not
  listed in any guide, listed without programmes, or listed with programmes outside the window.
  It also shows each guide's download status and channel counts, and guide channels with a
  similar name as clickable suggestions. Suggestions are never applied automatically.
* Channel ids in the guide are rewritten to the playlist's `tvg-id`, so clients match them
  exactly.
* Duplicate programmes are dropped. The window (days ahead, hours back) is set in Settings.
* When only the selection changes, the guide is rebuilt from the cached copy after a 15 s pause
  that collects further changes. Nothing is downloaded again.

## Feeds

* `playlist.m3u`, `epg.xml` and `epg.xml.gz` under `/feed/<token>/`.
* Feeds are static files, written to a temp file and moved into place atomically. Clients
  never trigger upstream fetches and never see a half-written file.
* Responses carry an `ETag`. `epg.xml` is sent gzipped to clients that accept gzip.

## Architecture

* Python 3.13, standard library only: `http.server`, `sqlite3`, expat-based
  `ElementTree.iterparse`. One container runs one process.
* One background worker thread runs all downloads, parsing and feed builds, so jobs never
  overlap. HTTP handlers only queue work, and repeated requests merge into one job.
* The UI is vanilla JS and CSS with no build step (`aiolineup/static/`).

## Security

* **Admin UI:** signed session cookie (HttpOnly, `SameSite=Strict`, 30 days). Logins are
  rate-limited to 10 failures per 15 minutes. Every change needs a JSON body and an
  `X-Requested-With` header, and requests from another origin are refused, which blocks CSRF.
* **Feeds:** only the token in the path is needed, so clients can fetch without logging in.
  Tokens are compared in constant time and replaced with `/feed/***/` in the access log. Query
  strings are never logged.
* **Upstream fetching (SSRF):**
  * Only `http`/`https` URLs without embedded credentials are allowed.
  * Every hop, redirects included, is resolved and refused if any of its addresses is
    non-public (unless `ALLOW_PRIVATE_SOURCES=true`). For NAT64 and other IPv6 forms that embed
    an IPv4 address, the embedded address is checked.
  * The connection is pinned to the addresses that passed the check, which defeats DNS
    rebinding.
  * At most 5 redirects are followed.
  * Size limits are enforced while downloading and again while decompressing.
* **XML:** expat loads no external entities or DTDs and rejects entity-expansion bombs.
* **UI:** strict Content Security Policy with no inline scripts or styles. Upstream text is
  only ever inserted with `textContent`.
* **Server:** request bodies are capped at 1 MiB, socket timeouts are 30 s, and at most 32
  connections are handled at once.
* **Secrets** come only from the environment or files in the data directory.
