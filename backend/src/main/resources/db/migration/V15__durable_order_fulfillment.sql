CREATE TABLE fulfillment_task (
    order_id VARCHAR(36) PRIMARY KEY,
    request_key VARCHAR(96) NOT NULL UNIQUE,
    command_json TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    fence BIGINT NOT NULL DEFAULT 0,
    owner VARCHAR(64),
    lease_until TIMESTAMP(6) NULL,
    next_attempt_at TIMESTAMP(6) NULL,
    tracking_no VARCHAR(128),
    receipt_json TEXT,
    last_error VARCHAR(1000),
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_fulfillment_dispatch (status, next_attempt_at, lease_until),
    CONSTRAINT fk_fulfillment_order FOREIGN KEY (order_id) REFERENCES customer_order(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE fulfillment_attempt (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id VARCHAR(36) NOT NULL,
    fence BIGINT NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    detail VARCHAR(1000),
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_fulfillment_attempt_order (order_id, id),
    CONSTRAINT fk_fulfillment_attempt_order FOREIGN KEY (order_id) REFERENCES customer_order(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
