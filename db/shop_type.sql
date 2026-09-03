CREATE TABLE IF NOT EXISTS shop_type (
    id BIGINT PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    sort INT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

ALTER TABLE shop_type
    CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

INSERT INTO shop_type (id, name, sort)
VALUES
    (1, '美食', 1),
    (2, '咖啡', 2),
    (3, '电影', 3),
    (4, '酒店', 4),
    (5, '健身', 5)
ON DUPLICATE KEY UPDATE
    name = VALUES(name),
    sort = VALUES(sort);
