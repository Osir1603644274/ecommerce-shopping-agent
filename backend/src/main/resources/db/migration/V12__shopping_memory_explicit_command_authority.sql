-- V12 closes the V11 read-only-shadow write gap.  A browser-confirmed action
-- first receives a server-issued, owner-bound, short-lived consent grant.
-- The command ledger then makes an exact retry replayable without replaying a
-- memory write, audit record, outbox event, or projection revision.

ALTER TABLE user_shopping_memory
    DROP INDEX uk_memory_chain_version,
    ADD UNIQUE KEY uk_memory_v2_chain_version (
        owner_user_id, memory_category, product_category, recipient_scope,
        semantic_key, version
    );

CREATE TABLE user_memory_consent_grant (
    consent_event_id VARCHAR(64) PRIMARY KEY,
    owner_user_id VARCHAR(36) NOT NULL,
    command_id VARCHAR(64) NOT NULL,
    consent_action VARCHAR(32) NOT NULL,
    command_digest CHAR(64) NOT NULL,
    content_digest CHAR(64) NOT NULL,
    issued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP NULL,
    UNIQUE KEY uk_memory_consent_owner_command (owner_user_id, command_id),
    INDEX idx_memory_consent_expiry (owner_user_id, expires_at),
    CONSTRAINT fk_memory_consent_grant_owner
        FOREIGN KEY (owner_user_id) REFERENCES user_account(id),
    CONSTRAINT ck_memory_consent_action
        CHECK (consent_action IN ('remember', 'confirm_suggestion', 'forget', 'disable'))
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE user_memory_command_result (
    owner_user_id VARCHAR(36) NOT NULL,
    command_id VARCHAR(64) NOT NULL,
    request_digest CHAR(64) NOT NULL,
    operation VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    memory_id VARCHAR(36) NULL,
    memory_version INT NULL,
    memory_status VARCHAR(16) NULL,
    completed_at TIMESTAMP NULL,
    PRIMARY KEY (owner_user_id, command_id),
    CONSTRAINT fk_memory_command_result_owner
        FOREIGN KEY (owner_user_id) REFERENCES user_account(id),
    CONSTRAINT ck_memory_command_result_operation
        CHECK (operation IN ('write', 'update', 'revoke', 'suppress')),
    CONSTRAINT ck_memory_command_result_status
        CHECK (status IN ('PROCESSING', 'APPLIED'))
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE=utf8mb4_unicode_ci;
