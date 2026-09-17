ALTER TABLE inventory_reservation DROP INDEX uk_inventory_reservation_order;
ALTER TABLE inventory_reservation ADD CONSTRAINT uk_inventory_reservation_order_stock UNIQUE (order_id,stock_id);
ALTER TABLE inventory_reservation ADD COLUMN refunded_quantity INT NOT NULL DEFAULT 0;

CREATE TABLE order_line_allocation (
    order_id VARCHAR(36) NOT NULL,
    item_type VARCHAR(32) NOT NULL,
    item_id BIGINT NOT NULL,
    stock_id BIGINT NOT NULL,
    quantity INT NOT NULL,
    subtotal_minor BIGINT NOT NULL,
    discount_minor BIGINT NOT NULL,
    paid_minor BIGINT NOT NULL,
    refunded_quantity INT NOT NULL DEFAULT 0,
    refunded_minor BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY(order_id,item_id),
    CONSTRAINT fk_line_allocation_order FOREIGN KEY(order_id) REFERENCES customer_order(id),
    CONSTRAINT fk_line_allocation_stock FOREIGN KEY(stock_id) REFERENCES inventory_stock(id),
    CONSTRAINT ck_line_allocation CHECK(quantity>0 AND refunded_quantity>=0 AND refunded_quantity<=quantity
        AND subtotal_minor>=0 AND discount_minor>=0 AND paid_minor>=0
        AND subtotal_minor=discount_minor+paid_minor AND refunded_minor>=0 AND refunded_minor<=paid_minor)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE fulfillment_command_version (
    order_id VARCHAR(36) NOT NULL,
    revision BIGINT NOT NULL,
    request_key VARCHAR(96) NOT NULL UNIQUE,
    command_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(order_id,revision),
    CONSTRAINT fk_fulfillment_command_order FOREIGN KEY(order_id) REFERENCES customer_order(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE partial_refund (
    id VARCHAR(36) PRIMARY KEY,
    order_id VARCHAR(36) NOT NULL,
    payment_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    amount_minor BIGINT NOT NULL,
    currency VARCHAR(16) NOT NULL,
    reason VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    provider_refund_no VARCHAR(128),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    refunded_at TIMESTAMP NULL,
    CONSTRAINT uk_partial_refund_key UNIQUE(order_id,idempotency_key),
    CONSTRAINT fk_partial_refund_order FOREIGN KEY(order_id) REFERENCES customer_order(id),
    CONSTRAINT fk_partial_refund_payment FOREIGN KEY(payment_id) REFERENCES payment_record(id),
    CONSTRAINT ck_partial_refund_amount CHECK(amount_minor>=0)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_partial_refund_status ON partial_refund(order_id,status);

CREATE TABLE partial_refund_item (
    refund_id VARCHAR(36) NOT NULL,
    item_id BIGINT NOT NULL,
    quantity INT NOT NULL,
    amount_minor BIGINT NOT NULL,
    PRIMARY KEY(refund_id,item_id),
    CONSTRAINT fk_partial_refund_item FOREIGN KEY(refund_id) REFERENCES partial_refund(id),
    CONSTRAINT ck_partial_refund_item CHECK(quantity>0 AND amount_minor>=0)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE local_refund_receipt (
    refund_id VARCHAR(36) PRIMARY KEY,
    provider_refund_no VARCHAR(128) NOT NULL UNIQUE,
    payment_id VARCHAR(36) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    amount_minor BIGINT NOT NULL,
    currency VARCHAR(16) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_local_refund_receipt FOREIGN KEY(refund_id) REFERENCES partial_refund(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
