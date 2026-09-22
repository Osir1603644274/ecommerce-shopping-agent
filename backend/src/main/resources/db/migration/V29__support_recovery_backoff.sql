ALTER TABLE support_case ADD COLUMN recovery_attempts INT NOT NULL DEFAULT 0;
ALTER TABLE support_case ADD COLUMN recovery_next_at TIMESTAMP(6) NULL;
ALTER TABLE support_case ADD COLUMN recovery_error VARCHAR(512) NULL;
ALTER TABLE support_stock_effect ADD COLUMN next_attempt_at TIMESTAMP(6) NULL;
