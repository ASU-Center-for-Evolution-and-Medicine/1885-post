ALTER TABLE newsletters ADD COLUMN quarantined_at TEXT;

CREATE TABLE IF NOT EXISTS sender_allowlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
