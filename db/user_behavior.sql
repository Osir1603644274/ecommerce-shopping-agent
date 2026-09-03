DROP TABLE IF EXISTS user_behavior;

CREATE TABLE user_behavior (
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
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
