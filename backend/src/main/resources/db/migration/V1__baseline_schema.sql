CREATE TABLE IF NOT EXISTS shop_type (
    id BIGINT PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    sort INT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS shop (
    id BIGINT PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    type_id BIGINT NOT NULL,
    address VARCHAR(255) NOT NULL,
    avg_price INT NOT NULL,
    phone VARCHAR(32),
    longitude DOUBLE NOT NULL,
    latitude DOUBLE NOT NULL,
    coordinate_system VARCHAR(16) NOT NULL DEFAULT 'BD-09',
    district VARCHAR(32),
    anchor_place_id VARCHAR(64),
    anchor_place_name VARCHAR(128),
    data_nature VARCHAR(32) NOT NULL DEFAULT 'synthetic_seed',
    source VARCHAR(32) NOT NULL DEFAULT 'seed',
    source_entity_id VARCHAR(128),
    original_name VARCHAR(128),
    original_address VARCHAR(255),
    original_longitude DOUBLE,
    original_latitude DOUBLE,
    localization_version VARCHAR(32),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_shop_type_id (type_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS review (
    id VARCHAR(64) PRIMARY KEY,
    shop_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    tags VARCHAR(255) NOT NULL,
    source VARCHAR(32) NOT NULL DEFAULT 'seed',
    language VARCHAR(16) NOT NULL DEFAULT 'zh',
    content_zh TEXT,
    translation_status VARCHAR(32) NOT NULL DEFAULT 'not_required',
    source_review_id VARCHAR(128),
    source_user_id VARCHAR(128),
    stars DOUBLE,
    source_shop_name VARCHAR(128),
    evidence_scope VARCHAR(96),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_review_shop_id (shop_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_behavior (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    shop_id BIGINT NOT NULL,
    behavior_type VARCHAR(32) NOT NULL,
    score DOUBLE,
    source VARCHAR(32) NOT NULL,
    source_user_id VARCHAR(128),
    source_review_id VARCHAR(128),
    occurred_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user_behavior_user_id (user_id),
    INDEX idx_user_behavior_shop_id (shop_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS product (
    id BIGINT PRIMARY KEY,
    source VARCHAR(64) NOT NULL,
    source_item_id VARCHAR(128) NOT NULL,
    title VARCHAR(512) NOT NULL,
    brand VARCHAR(128) NOT NULL,
    seller VARCHAR(255) NOT NULL,
    category_l1 VARCHAR(128) NOT NULL,
    category_l2 VARCHAR(128) NOT NULL,
    category_l3 VARCHAR(128) NOT NULL,
    snapshot_price_minor BIGINT,
    currency VARCHAR(16),
    price_status VARCHAR(32) NOT NULL DEFAULT 'missing',
    attribute_text TEXT,
    data_nature VARCHAR(64) NOT NULL DEFAULT 'historical_dataset_snapshot',
    dataset_revision VARCHAR(128) NOT NULL,
    source_license VARCHAR(64) NOT NULL,
    provenance_url VARCHAR(512) NOT NULL,
    imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_product_source_item (source, source_item_id),
    INDEX idx_product_category_l3 (category_l3),
    INDEX idx_product_brand (brand),
    INDEX idx_product_price (price_status, snapshot_price_minor)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS product_attribute (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    product_id BIGINT NOT NULL,
    attribute_key VARCHAR(64) NOT NULL,
    value_type VARCHAR(16) NOT NULL,
    raw_value VARCHAR(512) NOT NULL,
    normalized_text VARCHAR(255),
    normalized_number DECIMAL(18, 4),
    normalized_boolean BOOLEAN,
    unit VARCHAR(32),
    evidence_field VARCHAR(64) NOT NULL,
    extraction_method VARCHAR(64) NOT NULL,
    confidence DECIMAL(5, 4) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_product_attribute (product_id, attribute_key),
    INDEX idx_product_attribute_product (product_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
