-- Forward-only migration: operational rollback disables the feature flag and
-- stops writes; it must not drop user-memory tables or erase audit history.
CREATE TABLE user_shopping_memory (
    id VARCHAR(36) PRIMARY KEY,
    owner_user_id VARCHAR(36) NOT NULL,
    category VARCHAR(32) NOT NULL,
    semantic_key VARCHAR(64) NOT NULL,
    token_value VARCHAR(64) NOT NULL,
    version INT NOT NULL,
    status VARCHAR(16) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    supersedes_id VARCHAR(36),
    command_digest CHAR(64) NOT NULL,
    content_digest CHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_memory_chain_version (owner_user_id, category, semantic_key, version),
    INDEX idx_memory_projection (owner_user_id, status, expires_at, category, semantic_key),
    CONSTRAINT fk_memory_owner FOREIGN KEY (owner_user_id) REFERENCES user_account(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE user_memory_consent_consumption (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    owner_user_id VARCHAR(36) NOT NULL,
    consent_event_id VARCHAR(128) NOT NULL,
    command_digest CHAR(64) NOT NULL,
    content_digest CHAR(64) NOT NULL,
    consumed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_memory_consent_once (owner_user_id, consent_event_id),
    CONSTRAINT fk_memory_consent_owner FOREIGN KEY (owner_user_id) REFERENCES user_account(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE user_memory_audit (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    owner_user_id VARCHAR(36) NOT NULL,
    memory_id VARCHAR(36) NOT NULL,
    operation VARCHAR(16) NOT NULL,
    outcome VARCHAR(16) NOT NULL,
    command_digest CHAR(64) NOT NULL,
    consent_event_id VARCHAR(128) NOT NULL,
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_memory_audit_owner (owner_user_id, occurred_at),
    CONSTRAINT fk_memory_audit_owner FOREIGN KEY (owner_user_id) REFERENCES user_account(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE=utf8mb4_unicode_ci;
