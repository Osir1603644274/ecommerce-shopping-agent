CREATE TABLE support_ticket (
 id VARCHAR(36) PRIMARY KEY,
 order_id VARCHAR(36) NOT NULL,
 user_id VARCHAR(36) NOT NULL,
 case_id VARCHAR(36) NULL,
 category VARCHAR(40) NOT NULL,
 status VARCHAR(24) NOT NULL DEFAULT 'OPEN',
 summary VARCHAR(1000) NOT NULL,
 create_key VARCHAR(128) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 version BIGINT NOT NULL DEFAULT 0,
 created_at TIMESTAMP(6) NOT NULL,
 updated_at TIMESTAMP(6) NOT NULL,
 CONSTRAINT uk_ticket_create UNIQUE(order_id,create_key),
 CONSTRAINT fk_ticket_order FOREIGN KEY(order_id) REFERENCES customer_order(id),
 CONSTRAINT fk_ticket_case FOREIGN KEY(case_id) REFERENCES support_case(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_ticket_owner ON support_ticket(user_id,created_at,id);
CREATE TABLE support_ticket_event (
 id VARCHAR(36) PRIMARY KEY,
 ticket_id VARCHAR(36) NOT NULL,
 event_key VARCHAR(128) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 action VARCHAR(40) NOT NULL,
 message VARCHAR(2000) NOT NULL,
 created_at TIMESTAMP(6) NOT NULL,
 CONSTRAINT uk_ticket_event UNIQUE(ticket_id,event_key),
 CONSTRAINT fk_ticket_event FOREIGN KEY(ticket_id) REFERENCES support_ticket(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
