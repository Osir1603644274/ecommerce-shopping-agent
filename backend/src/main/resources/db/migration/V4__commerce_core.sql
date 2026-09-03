CREATE TABLE inventory_stock (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    item_type VARCHAR(32) NOT NULL,
    item_id BIGINT NOT NULL,
    total_quantity INT NOT NULL,
    available_quantity INT NOT NULL,
    reserved_quantity INT NOT NULL DEFAULT 0,
    sold_quantity INT NOT NULL DEFAULT 0,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_inventory_item (item_type, item_id),
    CONSTRAINT ck_inventory_non_negative CHECK (
        total_quantity >= 0 AND available_quantity >= 0
        AND reserved_quantity >= 0 AND sold_quantity >= 0
    )
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE inventory_reservation (
    id VARCHAR(36) PRIMARY KEY,
    order_id VARCHAR(36) NOT NULL,
    stock_id BIGINT NOT NULL,
    quantity INT NOT NULL,
    status VARCHAR(32) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_inventory_reservation_order (order_id),
    INDEX idx_inventory_reservation_expiry (status, expires_at),
    CONSTRAINT fk_inventory_reservation_stock
        FOREIGN KEY (stock_id) REFERENCES inventory_stock(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE coupon_template (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(128) NOT NULL,
    threshold_minor BIGINT NOT NULL DEFAULT 0,
    discount_minor BIGINT NOT NULL,
    total_quantity INT NOT NULL,
    claimed_quantity INT NOT NULL DEFAULT 0,
    valid_from TIMESTAMP NOT NULL,
    valid_until TIMESTAMP NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT ck_coupon_template_amount CHECK (
        threshold_minor >= 0 AND discount_minor > 0
        AND total_quantity >= 0 AND claimed_quantity >= 0
    )
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE user_coupon (
    id VARCHAR(36) PRIMARY KEY,
    template_id BIGINT NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'AVAILABLE',
    order_id VARCHAR(36),
    claimed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    used_at TIMESTAMP NULL,
    version BIGINT NOT NULL DEFAULT 0,
    UNIQUE KEY uk_user_coupon_template (template_id, user_id),
    INDEX idx_user_coupon_user_status (user_id, status),
    CONSTRAINT fk_user_coupon_template
        FOREIGN KEY (template_id) REFERENCES coupon_template(id),
    CONSTRAINT fk_user_coupon_user
        FOREIGN KEY (user_id) REFERENCES user_account(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE customer_order (
    id VARCHAR(36) PRIMARY KEY,
    order_no VARCHAR(32) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    total_minor BIGINT NOT NULL,
    discount_minor BIGINT NOT NULL DEFAULT 0,
    payable_minor BIGINT NOT NULL,
    currency VARCHAR(16) NOT NULL DEFAULT 'CNY',
    user_coupon_id VARCHAR(36),
    expires_at TIMESTAMP NOT NULL,
    paid_at TIMESTAMP NULL,
    completed_at TIMESTAMP NULL,
    cancelled_at TIMESTAMP NULL,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_customer_order_no (order_no),
    UNIQUE KEY uk_customer_order_idempotency (user_id, idempotency_key),
    INDEX idx_customer_order_user_created (user_id, created_at),
    INDEX idx_customer_order_expiry (status, expires_at),
    CONSTRAINT ck_customer_order_amount CHECK (
        total_minor >= 0 AND discount_minor >= 0 AND payable_minor >= 0
    ),
    CONSTRAINT fk_customer_order_user
        FOREIGN KEY (user_id) REFERENCES user_account(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE order_item (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id VARCHAR(36) NOT NULL,
    item_type VARCHAR(32) NOT NULL,
    item_id BIGINT NOT NULL,
    title_snapshot VARCHAR(512) NOT NULL,
    unit_price_minor BIGINT NOT NULL,
    quantity INT NOT NULL,
    subtotal_minor BIGINT NOT NULL,
    evidence_json JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_order_item_order (order_id),
    CONSTRAINT fk_order_item_order
        FOREIGN KEY (order_id) REFERENCES customer_order(id) ON DELETE CASCADE,
    CONSTRAINT ck_order_item_values CHECK (
        unit_price_minor >= 0 AND quantity > 0 AND subtotal_minor >= 0
    )
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE payment_record (
    id VARCHAR(36) PRIMARY KEY,
    payment_no VARCHAR(32) NOT NULL,
    order_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    amount_minor BIGINT NOT NULL,
    currency VARCHAR(16) NOT NULL DEFAULT 'CNY',
    provider_trade_no VARCHAR(128),
    paid_at TIMESTAMP NULL,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_payment_no (payment_no),
    UNIQUE KEY uk_payment_order (order_id),
    UNIQUE KEY uk_payment_provider_trade (provider, provider_trade_no),
    INDEX idx_payment_user_created (user_id, created_at),
    CONSTRAINT fk_payment_order
        FOREIGN KEY (order_id) REFERENCES customer_order(id),
    CONSTRAINT fk_payment_user
        FOREIGN KEY (user_id) REFERENCES user_account(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE payment_notification (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    provider VARCHAR(32) NOT NULL,
    event_id VARCHAR(128) NOT NULL,
    payment_no VARCHAR(32) NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_payment_notification (provider, event_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE refund_record (
    id VARCHAR(36) PRIMARY KEY,
    refund_no VARCHAR(32) NOT NULL,
    payment_id VARCHAR(36) NOT NULL,
    order_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    amount_minor BIGINT NOT NULL,
    reason VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    provider_refund_no VARCHAR(128),
    refunded_at TIMESTAMP NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_refund_no (refund_no),
    UNIQUE KEY uk_refund_order (order_id),
    CONSTRAINT fk_refund_payment
        FOREIGN KEY (payment_id) REFERENCES payment_record(id),
    CONSTRAINT fk_refund_order
        FOREIGN KEY (order_id) REFERENCES customer_order(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
