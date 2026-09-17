-- Idempotent existing-volume migration for the Beijing demo projection.
-- Copy the regenerated db/yelp_sample.sql to /tmp/yelp_sample.sql first, then:
-- mysql -uroot -p local_life < /tmp/beijing_localization_migration.sql
SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS add_column_if_missing;
DELIMITER //
CREATE PROCEDURE add_column_if_missing(
    IN requested_table VARCHAR(64),
    IN requested_column VARCHAR(64),
    IN column_definition TEXT
)
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = requested_table
          AND column_name = requested_column
    ) THEN
        SET @ddl = CONCAT(
            'ALTER TABLE `',
            requested_table,
            '` ADD COLUMN `',
            requested_column,
            '` ',
            column_definition
        );
        PREPARE statement FROM @ddl;
        EXECUTE statement;
        DEALLOCATE PREPARE statement;
    END IF;
END//
DELIMITER ;

CALL add_column_if_missing('shop', 'coordinate_system', 'VARCHAR(16) NOT NULL DEFAULT ''BD-09''');
CALL add_column_if_missing('shop', 'district', 'VARCHAR(32) NULL');
CALL add_column_if_missing('shop', 'anchor_place_id', 'VARCHAR(64) NULL');
CALL add_column_if_missing('shop', 'anchor_place_name', 'VARCHAR(128) NULL');
CALL add_column_if_missing('shop', 'data_nature', 'VARCHAR(32) NOT NULL DEFAULT ''synthetic_seed''');
CALL add_column_if_missing('shop', 'source', 'VARCHAR(32) NOT NULL DEFAULT ''seed''');
CALL add_column_if_missing('shop', 'source_entity_id', 'VARCHAR(128) NULL');
CALL add_column_if_missing('shop', 'original_name', 'VARCHAR(128) NULL');
CALL add_column_if_missing('shop', 'original_address', 'VARCHAR(255) NULL');
CALL add_column_if_missing('shop', 'original_longitude', 'DOUBLE NULL');
CALL add_column_if_missing('shop', 'original_latitude', 'DOUBLE NULL');
CALL add_column_if_missing('shop', 'localization_version', 'VARCHAR(32) NULL');
CALL add_column_if_missing('review', 'source_review_id', 'VARCHAR(128) NULL');
CALL add_column_if_missing('review', 'source_user_id', 'VARCHAR(128) NULL');
CALL add_column_if_missing('review', 'stars', 'DOUBLE NULL');
CALL add_column_if_missing('review', 'source_shop_name', 'VARCHAR(128) NULL');
CALL add_column_if_missing('review', 'evidence_scope', 'VARCHAR(96) NULL');
CALL add_column_if_missing('user_behavior', 'source_user_id', 'VARCHAR(128) NULL');
CALL add_column_if_missing('user_behavior', 'source_review_id', 'VARCHAR(128) NULL');

DROP PROCEDURE add_column_if_missing;

-- The import preserves shop/review/behavior IDs and updates display/geography
-- fields in place, so ItemCF similarities and user histories stay valid.
SOURCE /tmp/yelp_sample.sql;
