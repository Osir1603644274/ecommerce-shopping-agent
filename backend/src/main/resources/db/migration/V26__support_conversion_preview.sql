CREATE TABLE support_conversion_preview (
 id VARCHAR(36) PRIMARY KEY,
 case_id VARCHAR(36) NOT NULL,
 user_id VARCHAR(36) NOT NULL,
 expected_version BIGINT NOT NULL,
 amount_minor BIGINT NOT NULL,
 expires_at TIMESTAMP(6) NOT NULL,
 confirmed_key VARCHAR(128) NULL,
 CONSTRAINT fk_conversion_case FOREIGN KEY(case_id) REFERENCES support_case(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
