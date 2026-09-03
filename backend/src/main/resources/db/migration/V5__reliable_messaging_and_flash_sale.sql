CREATE TABLE outbox_event (
    id VARCHAR(36) PRIMARY KEY,
    aggregate_type VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    payload_json JSON NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lock_owner VARCHAR(64),
    locked_at TIMESTAMP NULL,
    published_at TIMESTAMP NULL,
    last_error VARCHAR(1000),
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_outbox_dispatch (status, next_attempt_at, occurred_at),
    INDEX idx_outbox_aggregate (aggregate_type, aggregate_id, occurred_at)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE inbox_event (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    consumer_name VARCHAR(128) NOT NULL,
    event_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    attempts INT NOT NULL DEFAULT 1,
    last_error VARCHAR(1000),
    claimed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_inbox_consumer_event (consumer_name, event_id),
    INDEX idx_inbox_recovery (status, claimed_at)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE dead_letter_event (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    source VARCHAR(64) NOT NULL,
    event_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    payload_json JSON NOT NULL,
    attempts INT NOT NULL,
    last_error VARCHAR(1000) NOT NULL,
    failed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_dead_letter_source_event (source, event_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE flash_sale_campaign (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    item_type VARCHAR(32) NOT NULL,
    item_id BIGINT NOT NULL,
    title VARCHAR(255) NOT NULL,
    sale_price_minor BIGINT NOT NULL,
    total_stock INT NOT NULL,
    available_stock INT NOT NULL,
    starts_at TIMESTAMP NOT NULL,
    ends_at TIMESTAMP NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'DRAFT',
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT ck_flash_sale_values CHECK (
        sale_price_minor >= 0 AND total_stock >= 0 AND available_stock >= 0
    )
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE flash_sale_order (
    id VARCHAR(36) PRIMARY KEY,
    campaign_id BIGINT NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING_PAYMENT',
    amount_minor BIGINT NOT NULL,
    stream_message_id VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_flash_sale_campaign_user (campaign_id, user_id),
    UNIQUE KEY uk_flash_sale_stream_message (stream_message_id),
    INDEX idx_flash_sale_order_user (user_id, created_at),
    CONSTRAINT fk_flash_sale_order_campaign
        FOREIGN KEY (campaign_id) REFERENCES flash_sale_campaign(id),
    CONSTRAINT fk_flash_sale_order_user
        FOREIGN KEY (user_id) REFERENCES user_account(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
