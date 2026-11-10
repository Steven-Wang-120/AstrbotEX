CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id <= 2147483647),
 username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
 display_name TEXT NOT NULL, bio TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL CHECK(kind IN ('human','bot'))
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 device_id TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('group','direct')),
 title TEXT NOT NULL, group_id INTEGER UNIQUE, direct_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS members (
 conversation_id TEXT NOT NULL REFERENCES conversations(id),
 user_id INTEGER NOT NULL REFERENCES users(id), role TEXT NOT NULL DEFAULT 'member',
 PRIMARY KEY(conversation_id,user_id)
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id <= 2147483647),
 conversation_id TEXT NOT NULL REFERENCES conversations(id),
 sender_id INTEGER NOT NULL REFERENCES users(id), client_message_id TEXT NOT NULL,
 segments TEXT NOT NULL, created_at TEXT NOT NULL, recalled INTEGER NOT NULL DEFAULT 0,
 UNIQUE(sender_id,client_message_id)
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
 event_id TEXT NOT NULL, type TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_account ON events(user_id,seq);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id,id);
CREATE TABLE IF NOT EXISTS bot_offsets (user_id INTEGER PRIMARY KEY REFERENCES users(id), seq INTEGER NOT NULL);
PRAGMA user_version=1;
