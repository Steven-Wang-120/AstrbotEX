ALTER TABLE media ADD COLUMN kind TEXT NOT NULL DEFAULT 'image';
ALTER TABLE media ADD COLUMN name TEXT NOT NULL DEFAULT 'image';
CREATE TABLE sticker_favorites (
 owner_id INTEGER NOT NULL REFERENCES users(id), sha256 TEXT NOT NULL,
 attachment TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,sha256)
);
PRAGMA user_version=3;
