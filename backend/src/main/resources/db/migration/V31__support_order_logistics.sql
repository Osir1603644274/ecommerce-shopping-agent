CREATE TABLE support_order_receipt (
 id VARCHAR(36) PRIMARY KEY,
 order_id VARCHAR(36) NOT NULL,
 event_type VARCHAR(24) NOT NULL,
 receipt_key VARCHAR(128) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 claim_json TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
 created_at TIMESTAMP(6) NOT NULL,
 applied_at TIMESTAMP(6) NULL,
 CONSTRAINT uk_order_receipt_event UNIQUE(order_id,event_type),
 CONSTRAINT uk_order_receipt_key UNIQUE(order_id,receipt_key),
 CONSTRAINT fk_order_receipt_order FOREIGN KEY(order_id) REFERENCES customer_order(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
