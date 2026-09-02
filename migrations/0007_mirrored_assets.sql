-- Records every externally-hosted image mirrored into R2, keyed by which newsletter it
-- belongs to and the exact source URL it came from. Two jobs: (1) the same "make repeat
-- runs incremental" cache resolved_links plays for tracked links -- a batched lookup
-- here tells a repeat ingest/Reprocess run exactly which candidate URLs already have a
-- mirrored copy, without re-spending external-fetch budget on them; (2) provenance for
-- undo/audit -- source_url is kept indefinitely, so which original address a given
-- mirrored image came from is always answerable, even if that address later goes dead.
CREATE TABLE IF NOT EXISTS mirrored_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    newsletter_slug TEXT NOT NULL,
    source_url TEXT NOT NULL,
    asset_key TEXT NOT NULL,   -- R2 object key suffix: newsletters/{newsletter_slug}/{asset_key}
    content_type TEXT,
    mirrored_at TEXT NOT NULL,
    UNIQUE(newsletter_slug, source_url)
);
CREATE INDEX IF NOT EXISTS idx_mirrored_assets_slug ON mirrored_assets(newsletter_slug);
