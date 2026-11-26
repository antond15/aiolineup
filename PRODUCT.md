# Product

## Platform

web (admin UI) plus two HTTP feeds (M3U, XMLTV) for media players.

## Users

One admin who self-hosts it. The only other consumer is AIOStreams' M3U addon fetching the feeds
with the token. There are no other users, roles, teams or sharing.

## Product Purpose

Public IPTV playlists and guides are huge and noisy; AIOStreams' M3U addon needs small, curated
feeds. AIOLineup lets the admin pick exactly the channels they want and serves a filtered
playlist and a matching guide at URLs that never change. It deliberately does not proxy,
download or transcode streams, and does not get around DRM, subscriptions or geo-blocking.

Success criteria:

- With both feed URLs added to AIOStreams' M3U addon, every selected channel shows up through
  AIOStreams with its alternatives grouped. Channels that the EPG page shows as matched have
  now/next guide data. A selection change appears within about 10 minutes without reinstalling
  the addon, and both feeds stay within the addon's size limits.
- The admin UI works equally well on a phone and on a desktop, and `DESIGN.md` describes it
  (tokens, typography, components) so later UI work can check itself against it.

## Positioning

- **Small and frugal.** One stdlib-only process that filters a 487 MiB guide in ~26 MiB RSS.
- **Nothing automatic that could be wrong.** No auto-selection, no name-based merging or EPG
  matching; similarity is only offered as a suggestion.

## Operating Context

- **Desktop browser or phone, occasional sessions:** the admin sets up sources, selects
  channels and fixes EPG mappings, then leaves it running for weeks. Every task works equally
  well on a 390 px phone and on a desktop.

## Capabilities and Constraints

- Areas: Overview (feed URLs, status), Channels, Sources, EPG, Settings.
- Hard constraints: 256 MiB container, feeds under the M3U addon's limits, single admin
  login, strict CSP, English UI, dark theme only (no light theme).
- Terminology: source, channel, stream (alternative), selection, guide/EPG, mapping, feed.

## Brand Commitments

- Name "AIOLineup", `aiolineup/static/icon.svg`. Plain, factual voice.
- Look: the category-standard dark dashboard, played straight, at the craft level of
  Sonarr/Radarr and Jellyfin; one indigo accent.

## Evidence on Hand

- Real iptv-org playlists and iptv-epg.org US/UK guides; memory measurements in the README.
  Test fixtures in `tests/fixtures/` are illustrative.

## Product Principles

1. **Never lose good data.** A failed refresh keeps the last good feed; a missing channel stays
   selected.
2. **Measured, not claimed.** Memory and size numbers come from real runs.
3. **Explain, don't guess.** When a channel has no guide, the UI says why and suggests, but never
   applies, a fix.

## Accessibility & Inclusion

WCAG 2.1 AA in every theme: text contrast ≥ 4.5:1, non-text ≥ 3:1, visible focus on every
control, full keyboard operation, touch targets ≥ 44 × 44 px, and color never the only signal.
Layouts hold at 200 % zoom.
