-- Replaces the fragile id-cursor bulk backfill used, which had no way to survive a
-- newsletter crashing its whole request (a Workers CPU-time-limit kill terminates the
-- isolate outright -- it cannot be caught in Python code, so the old cursor would never
-- advance past a newsletter like that, and every retry replayed the same crash forever).
-- backfill_attempts is written *before* each attempt, so it persists even through a hard
-- kill; selection always prefers never-attempted newsletters, so a newsletter that keeps
-- failing sinks behind fresh ones instead of blocking them.
ALTER TABLE newsletters ADD COLUMN backfilled_at TEXT;
ALTER TABLE newsletters ADD COLUMN backfill_attempts INTEGER NOT NULL DEFAULT 0;
