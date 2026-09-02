-- Thumbnail support: the largest externally-mirrored image for a newsletter (by byte
-- size) becomes its thumbnail candidate, shown next to it in every view. size_bytes on
-- mirrored_assets is what "largest" is computed from; thumbnail_key on newsletters
-- caches the winning asset_key so views don't need to recompute it on every render.
-- show_thumbnails on embed_queries lets a publisher opt an embed into showing them --
-- DEFAULT 0 means every existing embed keeps rendering exactly as it does today.
ALTER TABLE mirrored_assets ADD COLUMN size_bytes INTEGER;
ALTER TABLE newsletters ADD COLUMN thumbnail_key TEXT;
ALTER TABLE embed_queries ADD COLUMN show_thumbnails INTEGER NOT NULL DEFAULT 0;
