ALTER TABLE support_replacement ADD COLUMN tracking_no VARCHAR(128) NULL;
ALTER TABLE support_replacement ADD COLUMN dispatch_receipt_id VARCHAR(36) NULL;
ALTER TABLE support_replacement ADD COLUMN received_receipt_id VARCHAR(36) NULL;
