CREATE TABLE support_replacement (
 id VARCHAR(36) PRIMARY KEY,
 case_id VARCHAR(36) NOT NULL,
 case_version BIGINT NOT NULL,
 item_id BIGINT NOT NULL,
 quantity INT NOT NULL,
 specification TEXT NOT NULL,
 status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
 reserve_until TIMESTAMP(6) NOT NULL,
 created_at TIMESTAMP(6) NOT NULL,
 CONSTRAINT uk_replacement_attempt UNIQUE(case_id,case_version),
 CONSTRAINT fk_replacement_case FOREIGN KEY(case_id) REFERENCES support_case(id),
 CHECK(quantity>0)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
