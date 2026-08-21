CREATE TABLE IF NOT EXISTS embed_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    sender_email TEXT,
    result_limit INTEGER NOT NULL DEFAULT 5,
    sort TEXT NOT NULL DEFAULT 'newest',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embed_queries_token ON embed_queries(token);
