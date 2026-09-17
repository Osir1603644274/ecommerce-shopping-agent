-- Accepted flash-sale requests outlive the Redis notification stream.
CREATE TABLE flash_sale_request (
    id VARCHAR(36) PRIMARY KEY,
    campaign_id BIGINT NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    amount_minor BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    completed_order_id VARCHAR(36),
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error VARCHAR(1000),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_flash_request_buyer (campaign_id, user_id),
    INDEX idx_flash_request_recovery (status, next_attempt_at, created_at)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Serializes source-authoritative review projection snapshots, including deletions.
CREATE TABLE review_projection_head (
    review_id VARCHAR(64) PRIMARY KEY,
    revision BIGINT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
