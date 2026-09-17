ALTER TABLE flash_sale_request ADD COLUMN mq_published_at TIMESTAMP NULL;
ALTER TABLE flash_sale_request ADD COLUMN mq_next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE flash_sale_request ADD COLUMN mq_attempts INT NOT NULL DEFAULT 0;
CREATE INDEX idx_flash_request_dispatch ON flash_sale_request(status,mq_published_at,mq_next_attempt_at);
CREATE TABLE cache_invalidation_outbox (
 id VARCHAR(36) PRIMARY KEY,
 entity_type VARCHAR(16) NOT NULL,
 entity_id BIGINT NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 published_at TIMESTAMP NULL,
 next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 attempts INT NOT NULL DEFAULT 0,
 last_error VARCHAR(1000),
 INDEX idx_cache_dispatch(published_at,next_attempt_at)
);
