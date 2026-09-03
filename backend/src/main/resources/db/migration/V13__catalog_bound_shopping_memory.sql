-- V13 is additive. V12 endpoints and rows remain readable for rollback.
ALTER TABLE user_shopping_memory
    MODIFY COLUMN product_category VARCHAR(64) NOT NULL,
    MODIFY COLUMN token_value VARCHAR(128) NOT NULL,
    ADD COLUMN schema_version TINYINT NOT NULL DEFAULT 2 AFTER data_class,
    ADD COLUMN preference_kind VARCHAR(16) NOT NULL DEFAULT 'prefer' AFTER schema_version,
    ADD COLUMN catalog_revision VARCHAR(64) NOT NULL DEFAULT 'legacy-v12' AFTER preference_kind;

UPDATE user_shopping_memory
SET preference_kind = 'avoid'
WHERE semantic_key LIKE 'avoid\_%';

ALTER TABLE user_shopping_memory
    DROP CHECK ck_memory_v2_product_category,
    ADD CONSTRAINT ck_memory_v13_product_category
        CHECK (product_category REGEXP '^[a-z0-9][a-z0-9_-]{0,63}$'),
    ADD CONSTRAINT ck_memory_v13_schema_version
        CHECK (schema_version IN (2, 3)),
    ADD CONSTRAINT ck_memory_v13_preference_kind
        CHECK (preference_kind IN ('prefer', 'avoid', 'indifferent'));

CREATE TABLE shopping_memory_catalog_value (
    catalog_revision VARCHAR(64) NOT NULL,
    category_id VARCHAR(64) NOT NULL,
    attribute_key VARCHAR(64) NOT NULL,
    normalized_value VARCHAR(128) NOT NULL,
    display_label VARCHAR(256) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (catalog_revision, category_id, attribute_key, normalized_value),
    INDEX idx_memory_catalog_active (
        catalog_revision, category_id, attribute_key, active
    ),
    CONSTRAINT ck_memory_catalog_revision
        CHECK (catalog_revision REGEXP '^[A-Za-z0-9._:-]{1,64}$'),
    CONSTRAINT ck_memory_catalog_category
        CHECK (category_id REGEXP '^[a-z0-9][a-z0-9_-]{0,63}$'),
    CONSTRAINT ck_memory_catalog_attribute
        CHECK (attribute_key REGEXP '^[a-z][a-z0-9_]{0,63}$')
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX idx_memory_projection_v13 ON user_shopping_memory (
    owner_user_id, schema_version, product_category, recipient_scope,
    status, expires_at, updated_at
);
