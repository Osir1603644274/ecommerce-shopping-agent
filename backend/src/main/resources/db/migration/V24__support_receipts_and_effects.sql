CREATE TABLE support_receipt (
 id VARCHAR(36) PRIMARY KEY,
 case_id VARCHAR(36) NOT NULL,
 receipt_key VARCHAR(128) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 event_type VARCHAR(48) NOT NULL,
 expected_version BIGINT NOT NULL,
 actor VARCHAR(128) NOT NULL,
 payload_json TEXT NOT NULL,
 status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
 attempts INT NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMP(6) NULL,
 last_error VARCHAR(512) NULL,
 created_at TIMESTAMP(6) NOT NULL,
 applied_at TIMESTAMP(6) NULL,
 CONSTRAINT uk_support_receipt_key UNIQUE(case_id,receipt_key),
 CONSTRAINT fk_support_receipt_case FOREIGN KEY(case_id) REFERENCES support_case(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_support_receipt_pending ON support_receipt(status,created_at);
CREATE TABLE support_return (
 case_id VARCHAR(36) PRIMARY KEY,
 tracking_no VARCHAR(128) NOT NULL,
 item_id BIGINT NOT NULL,
 requested_quantity INT NOT NULL,
 received_quantity INT NULL,
 sellable BOOLEAN NULL,
 receipt_id VARCHAR(36) NULL,
 inspection_receipt_id VARCHAR(36) NULL,
 CONSTRAINT fk_support_return_case FOREIGN KEY(case_id) REFERENCES support_case(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE TABLE support_refund_command (
 case_id VARCHAR(36) PRIMARY KEY,
 payment_id VARCHAR(36) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 amount_minor BIGINT NOT NULL,
 currency VARCHAR(16) NOT NULL,
 status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
 provider_receipt_id VARCHAR(36) NULL,
 created_at TIMESTAMP(6) NOT NULL,
 completed_at TIMESTAMP(6) NULL,
 CONSTRAINT fk_support_refund_case FOREIGN KEY(case_id) REFERENCES support_case(id),
 CONSTRAINT fk_support_refund_payment FOREIGN KEY(payment_id) REFERENCES payment_record(id),
 CHECK(amount_minor>=0)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE TABLE support_stock_effect (
 effect_id VARCHAR(64) PRIMARY KEY,
 case_id VARCHAR(36) NOT NULL,
 order_id VARCHAR(36) NOT NULL,
 item_id BIGINT NOT NULL,
 quantity INT NOT NULL,
 kind VARCHAR(32) NOT NULL,
 status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
 receipt_id VARCHAR(128) NULL,
 attempts INT NOT NULL DEFAULT 0,
 last_error VARCHAR(512) NULL,
 created_at TIMESTAMP(6) NOT NULL,
 CONSTRAINT fk_support_stock_case FOREIGN KEY(case_id) REFERENCES support_case(id),
 CHECK(quantity>0 AND attempts>=0)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
