-- Duplver schema v0.1.
-- One database file per scanned root (see duplver.paths.root_key).
-- All detection stages have tables even when stubbed, so the schema is stable
-- as features come online.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    UNIQUE NOT NULL,
    root        TEXT    NOT NULL,
    filename    TEXT,
    extension   TEXT,
    size_bytes  INTEGER,
    mtime       REAL,
    ctime       REAL,
    width       INTEGER,
    height      INTEGER,
    format      TEXT,
    mode        TEXT,
    exif_json   TEXT,
    status      TEXT    NOT NULL DEFAULT 'discovered',
    error       TEXT,
    discovered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_files_root    ON files(root);
CREATE INDEX IF NOT EXISTS idx_files_status  ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_size    ON files(size_bytes);

CREATE TABLE IF NOT EXISTS exact_hashes (
    file_id     INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    sha256      TEXT    NOT NULL,
    size_bytes  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exact_sha     ON exact_hashes(sha256);

CREATE TABLE IF NOT EXISTS perceptual_hashes (
    file_id     INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    phash       TEXT,
    dhash       TEXT,
    ahash       TEXT,
    bit_length  INTEGER
);

CREATE TABLE IF NOT EXISTS embeddings (
    file_id     INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    model       TEXT    NOT NULL,
    pretrained  TEXT    NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB    NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_a      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    file_b      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    method      TEXT    NOT NULL,
    inliers     INTEGER,
    total       INTEGER,
    confidence  REAL,
    UNIQUE(file_a, file_b, method)
);
CREATE INDEX IF NOT EXISTS idx_features_a ON feature_matches(file_a);
CREATE INDEX IF NOT EXISTS idx_features_b ON feature_matches(file_b);

CREATE TABLE IF NOT EXISTS clusters (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT    NOT NULL,
    detection_method TEXT,
    confidence       REAL,
    best_file_id     INTEGER REFERENCES files(id) ON DELETE SET NULL,
    created_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_clusters_kind  ON clusters(kind);
CREATE INDEX IF NOT EXISTS idx_clusters_best  ON clusters(best_file_id);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id     INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    role           TEXT    NOT NULL,
    quality_score  REAL,
    PRIMARY KEY (cluster_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_members_file ON cluster_members(file_id);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    cluster_id  INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
    action      TEXT    NOT NULL,
    actor       TEXT    NOT NULL,
    at          REAL    NOT NULL,
    reverted    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_actions_file ON actions(file_id);

-- Trash audit trail. send2trash is best-effort and OS-dependent; we log
-- what we asked it to move so restore() can attempt reversal.
CREATE TABLE IF NOT EXISTS trashed (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    original_path   TEXT    NOT NULL,
    trashed_at      REAL    NOT NULL,
    platform        TEXT,
    restored        INTEGER NOT NULL DEFAULT 0
);