CREATE TABLE media (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
 conversation_id TEXT NOT NULL REFERENCES conversations(id), client_media_id TEXT NOT NULL,
 source_sha256 TEXT NOT NULL, sha256 TEXT NOT NULL, content_type TEXT NOT NULL,
 size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 8388608),
 width INTEGER NOT NULL, height INTEGER NOT NULL, content BLOB NOT NULL,
 UNIQUE(owner_id,client_media_id)
);
CREATE TABLE media_uses (
 media_id TEXT NOT NULL REFERENCES media(id), message_id INTEGER NOT NULL REFERENCES messages(id),
 PRIMARY KEY(media_id,message_id)
);
PRAGMA user_version=2;
