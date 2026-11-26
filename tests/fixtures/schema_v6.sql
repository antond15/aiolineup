-- The schema of a version 6 database, frozen from the version 6 db.py, so migration tests
-- start from the tables a real install has rather than from today's SCHEMA with parts removed.
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('m3u', 'xmltv')),
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 100,
    created_at REAL NOT NULL,
    last_attempt REAL,
    last_success REAL,
    last_error TEXT,
    etag TEXT,
    last_modified TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    fragment_key TEXT
);

-- Last successfully parsed playlist entries per M3U source.
CREATE TABLE IF NOT EXISTS streams (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    pos INTEGER NOT NULL,
    channel_key TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    duration TEXT NOT NULL,
    attrs TEXT NOT NULL,
    directives TEXT NOT NULL,
    PRIMARY KEY (source_id, pos)
);
CREATE INDEX IF NOT EXISTS streams_channel ON streams(channel_key);

-- Whitelist. Survives refreshes; rows stay even while a channel is missing upstream.
CREATE TABLE IF NOT EXISTS selections (
    channel_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    added_at REAL NOT NULL,
    -- Sets the feed order and is sent as tvg-chno; unique, gaps allowed.
    number INTEGER NOT NULL UNIQUE CHECK (number >= 1)
);

-- The admin's stream order and on/off for a selected channel, forgotten on deselect.
-- signature = m3u.signature_key(url, directives). A stream without a row goes after the
-- ranked ones in source order, on.
CREATE TABLE IF NOT EXISTS stream_prefs (
    channel_key TEXT NOT NULL,
    signature TEXT NOT NULL,
    position INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    PRIMARY KEY (channel_key, signature)
);

-- The admin's own name and logo for a channel (NULL = the source value). Kept on deselect,
-- like category_overrides and epg_mappings.
CREATE TABLE IF NOT EXISTS channel_edits (
    channel_key TEXT PRIMARY KEY,
    name TEXT,
    logo TEXT
);

-- Manual channel -> XMLTV id overrides. Empty epg_id = "no guide for this channel".
-- source_id = the guide the id was picked from: tried first, other guides follow by
-- priority (NULL = priority order only, e.g. a typed id or a deleted source).
CREATE TABLE IF NOT EXISTS epg_mappings (
    channel_key TEXT PRIMARY KEY,
    epg_id TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL
);

-- The admin's own category for a channel; replaces its source categories in the app and
-- becomes the group-title in the playlist feed. Keyed like selections, so it survives refreshes.
CREATE TABLE IF NOT EXISTS category_overrides (
    channel_key TEXT PRIMARY KEY,
    category TEXT NOT NULL
);

-- Channel index of the last successfully parsed version of each XMLTV source.
CREATE TABLE IF NOT EXISTS epg_channels (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    epg_id TEXT NOT NULL,
    epg_id_lc TEXT NOT NULL,
    display_name TEXT NOT NULL,
    programmes INTEGER NOT NULL DEFAULT 0,
    first_start INTEGER,  -- programme time span, tracked for wanted ids only
    last_stop INTEGER,
    PRIMARY KEY (source_id, epg_id_lc)
);
CREATE INDEX IF NOT EXISTS epg_channels_lc ON epg_channels(epg_id_lc);

-- Programmes of every guide channel from scan time to 48 hours later, for "now playing".
CREATE TABLE IF NOT EXISTS epg_schedule (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    epg_id_lc TEXT NOT NULL,
    start INTEGER NOT NULL,
    stop INTEGER NOT NULL,
    title TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS epg_schedule_ch ON epg_schedule(source_id, epg_id_lc, start);

-- Languages from the iptv-org API, keyed by lowercase tvg-id (with and without @feed).
CREATE TABLE IF NOT EXISTS meta_languages (
    tvg_id_lc TEXT PRIMARY KEY,
    languages TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
