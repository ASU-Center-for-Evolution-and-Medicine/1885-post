-- Cloudflare Workers caps total subrequests (fetch calls + D1 queries) per invocation,
-- and a newsletter can have 20-30+ tracked links -- resolving them all in one pass can
-- exceed that cap partway through. This cache lets resolution proceed incrementally:
-- each run resolves a capped batch of not-yet-cached links and persists successes
-- immediately, so a repeat Reprocess click picks up exactly where the last one left off
-- instead of re-spending budget re-resolving the same links from scratch.
CREATE TABLE IF NOT EXISTS resolved_links (
    tracked_url TEXT PRIMARY KEY,
    resolved_url TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);
