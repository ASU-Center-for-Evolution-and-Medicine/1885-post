ALTER TABLE newsletters ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public';

CREATE TABLE IF NOT EXISTS sender_settings (
    from_email TEXT PRIMARY KEY,
    default_visibility TEXT,
    share_key TEXT UNIQUE,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
