-- Catalog versioning for atomic BM25 index switches.
-- A new version is published only after a complete, verified product import.
CREATE TABLE IF NOT EXISTS catalog_state (
    catalog_version VARCHAR(128) NOT NULL PRIMARY KEY,
    product_count  INT          NOT NULL,
    content_hash   VARCHAR(128) NOT NULL,
    published_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Seed the current state from the existing product table so the first
-- Python CatalogIndexManager check has a baseline version.
INSERT IGNORE INTO catalog_state (catalog_version, product_count, content_hash)
SELECT
    dataset_revision,
    cnt,
    content_hash
FROM (
    SELECT
        dataset_revision,
        COUNT(1) AS cnt,
        SHA2(GROUP_CONCAT(id ORDER BY id SEPARATOR ','), 256) AS content_hash
    FROM product
    WHERE dataset_revision IS NOT NULL AND dataset_revision <> ''
    GROUP BY dataset_revision
    ORDER BY dataset_revision DESC
    LIMIT 1
) latest
WHERE latest.dataset_revision IS NOT NULL;
