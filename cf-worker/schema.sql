CREATE TABLE IF NOT EXISTS accounts (
  phone TEXT PRIMARY KEY,
  sess_key TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  level TEXT NOT NULL DEFAULT '',
  last_login INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sms_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  body TEXT NOT NULL,
  code TEXT,
  phone TEXT,
  digits INTEGER,
  received_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  consumed_at INTEGER
);
CREATE INDEX IF NOT EXISTS sms_messages_pending ON sms_messages(consumed_at, expires_at, received_at);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, phone TEXT NOT NULL, state TEXT NOT NULL,
  payload TEXT NOT NULL, updated_at INTEGER NOT NULL
);
