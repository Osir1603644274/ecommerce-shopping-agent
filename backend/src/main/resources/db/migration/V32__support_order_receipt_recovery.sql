ALTER TABLE support_order_receipt ADD COLUMN attempts INT NOT NULL DEFAULT 0;
ALTER TABLE support_order_receipt ADD COLUMN next_attempt_at TIMESTAMP(6) NULL;
ALTER TABLE support_order_receipt ADD COLUMN last_error VARCHAR(512) NULL;
