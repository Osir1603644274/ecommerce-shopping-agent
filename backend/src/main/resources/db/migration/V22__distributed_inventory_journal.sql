-- Trade-owned durable TCC intents and post-commit inventory command outbox.
-- No FK to order: TRY must survive a rolled-back order transaction.
CREATE TABLE inventory_command_journal (
 sequence_id BIGINT PRIMARY KEY AUTO_INCREMENT,
 command_id VARCHAR(128) NOT NULL,
 order_id VARCHAR(36) NOT NULL,
 kind VARCHAR(24) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 command_json JSON NOT NULL,
 status VARCHAR(24) NOT NULL,
 response_json JSON NULL,
 attempts INT NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 last_error VARCHAR(256) NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uk_inventory_command_journal(command_id),
 INDEX idx_inventory_command_due(status,next_attempt_at),
 INDEX idx_inventory_command_order(order_id,sequence_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
