CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor_email TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_log_created_at ON action_log(created_at);
CREATE INDEX IF NOT EXISTS idx_action_log_actor_action_created ON action_log(actor_email, action, created_at);
