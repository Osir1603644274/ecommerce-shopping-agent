CREATE TABLE product_favorite (
    user_id VARCHAR(64) NOT NULL,
    product_id BIGINT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, product_id),
    INDEX idx_product_favorite_user_time (user_id, created_at, product_id),
    CONSTRAINT fk_product_favorite_product FOREIGN KEY (product_id) REFERENCES product(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
