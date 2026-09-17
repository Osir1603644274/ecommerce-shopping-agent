-- Import receipts are separate from source claims, prices and mutable stock.
CREATE TABLE IF NOT EXISTS external_catalog_identity (
    product_id BIGINT NOT NULL PRIMARY KEY,
    raw_sha BINARY(32) NOT NULL,
    source_line BIGINT NOT NULL,
    source_revision CHAR(64) NOT NULL,
    import_version VARCHAR(128) NOT NULL,
    preserved_existing BOOLEAN NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS external_catalog_import_checkpoint (
    source VARCHAR(64) NOT NULL PRIMARY KEY,
    input_sha CHAR(64) NOT NULL,
    rule_version VARCHAR(128) NOT NULL,
    byte_offset BIGINT NOT NULL DEFAULT 0,
    source_line BIGINT NOT NULL DEFAULT 0,
    inserted_count BIGINT NOT NULL DEFAULT 0,
    preserved_count BIGINT NOT NULL DEFAULT 0,
    quarantined_count BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'IMPORTING',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS external_catalog_quarantine (
    source VARCHAR(64) NOT NULL,
    source_line BIGINT NOT NULL,
    raw_sha BINARY(32) NOT NULL,
    reason VARCHAR(1024) NOT NULL,
    PRIMARY KEY(source,source_line)
);

-- Freeze published legacy retrieval membership. Newly imported trading rows must
-- not silently change a previously published 439-phone index under its old hash.
CREATE TABLE IF NOT EXISTS catalog_version_member (
    catalog_version VARCHAR(128) NOT NULL,
    product_id BIGINT NOT NULL,
    PRIMARY KEY(catalog_version, product_id)
);
INSERT IGNORE INTO catalog_version_member(catalog_version,product_id)
SELECT cs.catalog_version,p.id FROM catalog_state cs CROSS JOIN product p
WHERE p.lifecycle_status='ACTIVE'
AND NOT EXISTS (SELECT 1 FROM catalog_version_member cm WHERE BINARY cm.catalog_version=BINARY cs.catalog_version)
AND cs.catalog_version=(SELECT latest.catalog_version FROM
    (SELECT catalog_version FROM catalog_state ORDER BY published_at DESC LIMIT 1) latest);
