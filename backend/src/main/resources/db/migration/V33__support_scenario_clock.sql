CREATE TABLE support_scenario_clock (
 order_id VARCHAR(36) PRIMARY KEY,
 virtual_now TIMESTAMP(6) NOT NULL,
 version BIGINT NOT NULL DEFAULT 0,
 actor VARCHAR(128) NOT NULL,
 CONSTRAINT fk_scenario_clock_order FOREIGN KEY(order_id) REFERENCES customer_order(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE TABLE support_clock_event (
 order_id VARCHAR(36) NOT NULL,
 event_key VARCHAR(128) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 actor VARCHAR(128) NOT NULL,
 from_time TIMESTAMP(6) NOT NULL,
 to_time TIMESTAMP(6) NOT NULL,
 PRIMARY KEY(order_id,event_key),
 CONSTRAINT fk_clock_event_order FOREIGN KEY(order_id) REFERENCES customer_order(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
