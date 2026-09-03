-- P0 forward-only hardening. V8/V9 remain immutable for rollback and audit.
-- Existing rows cannot be assumed to be explicit, self-scoped product memory.
ALTER TABLE user_shopping_memory
    ADD COLUMN memory_category VARCHAR(32) NOT NULL DEFAULT 'shopping_preference' AFTER owner_user_id,
    ADD COLUMN product_category VARCHAR(32) NOT NULL DEFAULT 'unknown' AFTER memory_category,
    ADD COLUMN recipient_scope VARCHAR(16) NOT NULL DEFAULT 'unknown' AFTER product_category,
    ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT 'legacy_unverified' AFTER recipient_scope,
    ADD COLUMN data_class VARCHAR(32) NOT NULL DEFAULT 'long_term_preference' AFTER source,
    ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP AFTER created_at,
    ADD INDEX idx_memory_projection_v2 (
        owner_user_id, data_class, memory_category, product_category,
        recipient_scope, source, status, expires_at, updated_at
    );

-- Sensitive legacy rows are retained for audit, but can never enter either new
-- writes or the v2 projection. No migration invents product/recipient/source.
INSERT INTO user_memory_audit(
    owner_user_id, memory_id, operation, outcome,
    command_digest, consent_event_id
)
SELECT owner_user_id, id, 'MIGRATE_V11', 'SUPPRESSED',
       command_digest, CONCAT('migration-v11-', id)
FROM user_shopping_memory
WHERE category IN ('health', 'allergy');

UPDATE user_shopping_memory
SET status = 'LEGACY_REJECTED'
WHERE category IN ('health', 'allergy');

-- Any changed lifecycle state invalidates a pre-migration Redis envelope.
INSERT INTO user_memory_projection_head(owner_user_id, revision)
SELECT owner_user_id, 1
FROM user_shopping_memory
WHERE category IN ('health', 'allergy')
GROUP BY owner_user_id
ON DUPLICATE KEY UPDATE revision = revision + 1;

ALTER TABLE user_shopping_memory
    ADD CONSTRAINT ck_memory_v2_memory_category
        CHECK (memory_category = 'shopping_preference'),
    ADD CONSTRAINT ck_memory_v2_product_category
        CHECK (product_category IN ('unknown', 'phone', 'laptop', 'headphones')),
    ADD CONSTRAINT ck_memory_v2_recipient_scope
        CHECK (recipient_scope IN ('unknown', 'self')),
    ADD CONSTRAINT ck_memory_v2_source
        CHECK (source IN ('legacy_unverified', 'explicit_user', 'user_confirmed')),
    ADD CONSTRAINT ck_memory_v2_data_class
        CHECK (data_class = 'long_term_preference');
