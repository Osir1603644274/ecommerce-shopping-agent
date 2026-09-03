-- MySQL remains authoritative: this monotonically increasing head invalidates
-- any Redis envelope whose revision was produced before a committed write.
CREATE TABLE user_memory_projection_head (
    owner_user_id VARCHAR(36) PRIMARY KEY,
    revision BIGINT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_memory_projection_head_owner FOREIGN KEY (owner_user_id) REFERENCES user_account(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Existing V8 chains receive a non-zero authoritative revision during upgrade.
INSERT INTO user_memory_projection_head(owner_user_id, revision)
SELECT owner_user_id, COUNT(*)
FROM user_shopping_memory
GROUP BY owner_user_id;
