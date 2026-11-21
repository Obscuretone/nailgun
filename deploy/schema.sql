CREATE TABLE IF NOT EXISTS queue_lock (
    id TINYINT PRIMARY KEY
) ENGINE=InnoDB;

INSERT IGNORE INTO queue_lock (id) VALUES (1);

CREATE TABLE IF NOT EXISTS domains (
    name VARCHAR(253) PRIMARY KEY,
    enabled TINYINT NOT NULL DEFAULT 0,
    google_verified TINYINT NOT NULL DEFAULT 0,
    present TINYINT NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    updated_at DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

CREATE TABLE IF NOT EXISTS messages (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    message_id VARCHAR(320) NOT NULL,
    payload LONGTEXT NOT NULL,
    digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    idempotency_key VARCHAR(200) UNIQUE,
    domain VARCHAR(253) NOT NULL,
    recipients INT NOT NULL,
    status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    next_attempt DOUBLE NOT NULL,
    error TEXT,
    accepted TEXT,
    INDEX ready_messages (status, next_attempt),
    INDEX created_messages (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;
