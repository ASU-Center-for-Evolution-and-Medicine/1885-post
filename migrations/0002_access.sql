CREATE TABLE IF NOT EXISTS email_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    to_address TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_email, to_address)
);
CREATE INDEX IF NOT EXISTS idx_email_access_user_email ON email_access(user_email);
