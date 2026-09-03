ALTER TABLE product
    ADD COLUMN lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE' AFTER price_status,
    ADD COLUMN entity_version BIGINT NOT NULL DEFAULT 1 AFTER lifecycle_status,
    ADD INDEX idx_product_search_lifecycle (lifecycle_status, entity_version);

CREATE TABLE product_search_projection_cursor (
    product_id BIGINT PRIMARY KEY,
    entity_version BIGINT NOT NULL,
    event_id VARCHAR(36) NOT NULL,
    operation VARCHAR(16) NOT NULL,
    indexed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_product_search_projection_event (event_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
