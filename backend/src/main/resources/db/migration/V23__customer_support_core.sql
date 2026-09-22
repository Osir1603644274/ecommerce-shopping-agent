-- New support writes stay feature-gated until their adapters and recovery are verified.
CREATE TABLE support_case (
 id VARCHAR(36) PRIMARY KEY,
 order_id VARCHAR(36) NOT NULL,
 user_id VARCHAR(36) NOT NULL,
 item_id BIGINT NOT NULL,
 quantity INT NOT NULL,
 original_type VARCHAR(32) NOT NULL,
 current_type VARCHAR(32) NOT NULL,
 phase VARCHAR(40) NOT NULL,
 amount_minor BIGINT NOT NULL,
 currency VARCHAR(16) NOT NULL,
 reason VARCHAR(1000) NOT NULL,
 specification_json TEXT NULL,
 policy_version VARCHAR(64) NOT NULL,
 idempotency_key VARCHAR(128) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 version BIGINT NOT NULL DEFAULT 0,
 created_at TIMESTAMP(6) NOT NULL,
 updated_at TIMESTAMP(6) NOT NULL,
 CONSTRAINT uk_support_case_key UNIQUE(order_id,idempotency_key),
 CONSTRAINT fk_support_case_order FOREIGN KEY(order_id) REFERENCES customer_order(id),
 CONSTRAINT ck_support_case_values CHECK(quantity>0 AND amount_minor>=0 AND version>=0)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_support_case_owner ON support_case(user_id,created_at,id);
CREATE TABLE support_order_claim (
 order_id VARCHAR(36) PRIMARY KEY,
 case_id VARCHAR(36) NOT NULL UNIQUE,
 quantity INT NOT NULL,
 amount_minor BIGINT NOT NULL,
 CONSTRAINT fk_support_claim_case FOREIGN KEY(case_id) REFERENCES support_case(id),
 CONSTRAINT ck_support_claim_values CHECK(quantity>0 AND amount_minor>=0)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE TABLE support_preview (
 id VARCHAR(36) PRIMARY KEY,
 order_id VARCHAR(36) NOT NULL,
 user_id VARCHAR(36) NOT NULL,
 request_json TEXT NOT NULL,
 facts_hash CHAR(64) NOT NULL,
 amount_minor BIGINT NOT NULL,
 expires_at TIMESTAMP(6) NOT NULL,
 case_id VARCHAR(36) NULL,
 CONSTRAINT fk_support_preview_order FOREIGN KEY(order_id) REFERENCES customer_order(id),
 CONSTRAINT fk_support_preview_case FOREIGN KEY(case_id) REFERENCES support_case(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE TABLE support_event (
 id VARCHAR(36) PRIMARY KEY,
 case_id VARCHAR(36) NOT NULL,
 event_key VARCHAR(128) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 event_type VARCHAR(48) NOT NULL,
 from_phase VARCHAR(40) NULL,
 to_phase VARCHAR(40) NOT NULL,
 evidence_json TEXT NOT NULL,
 created_at TIMESTAMP(6) NOT NULL,
 CONSTRAINT uk_support_event_key UNIQUE(case_id,event_key),
 CONSTRAINT fk_support_event_case FOREIGN KEY(case_id) REFERENCES support_case(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_support_event_case ON support_event(case_id,created_at,id);
CREATE TABLE support_sale_specification (
 product_id BIGINT PRIMARY KEY,
 code VARCHAR(128) NOT NULL,
 label VARCHAR(512) NOT NULL,
 version BIGINT NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT TRUE,
 CHECK(version>0)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
