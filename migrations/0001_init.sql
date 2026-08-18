CREATE TABLE IF NOT EXISTS newsletters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    from_address TEXT NOT NULL,
    to_address TEXT NOT NULL,
    subject TEXT NOT NULL,
    received_at TEXT,
    slug TEXT NOT NULL UNIQUE,
    raw_eml BLOB NOT NULL,
    sanitized_html TEXT,
    plain_text_fallback TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_newsletters_received_at ON newsletters(received_at);
CREATE INDEX IF NOT EXISTS idx_newsletters_from_address ON newsletters(from_address);
CREATE INDEX IF NOT EXISTS idx_newsletters_to_address ON newsletters(to_address);

CREATE TABLE IF NOT EXISTS newsletter_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    newsletter_id INTEGER NOT NULL REFERENCES newsletters(id),
    content_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data BLOB NOT NULL,
    UNIQUE(newsletter_id, content_id)
);
