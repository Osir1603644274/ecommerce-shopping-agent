-- This schema owns inventory exclusively; no foreign key to another service.
CREATE TABLE inventory_stock (
 id BIGINT PRIMARY KEY AUTO_INCREMENT, item_type VARCHAR(32) NOT NULL, item_id BIGINT NOT NULL,
 total_quantity INT NOT NULL, available_quantity INT NOT NULL,
 reserved_quantity INT NOT NULL DEFAULT 0, sold_quantity INT NOT NULL DEFAULT 0,
 version BIGINT NOT NULL DEFAULT 0,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uk_inventory_item(item_type,item_id),
 CONSTRAINT ck_stock_balance CHECK(total_quantity=available_quantity+reserved_quantity+sold_quantity),
 CONSTRAINT ck_stock_nonnegative CHECK(available_quantity>=0 AND reserved_quantity>=0 AND sold_quantity>=0)
) ENGINE=InnoDB;
CREATE TABLE inventory_reservation (
 id VARCHAR(36) PRIMARY KEY, order_id VARCHAR(36) NOT NULL, stock_id BIGINT NOT NULL,
 quantity INT NOT NULL, refunded_quantity INT NOT NULL DEFAULT 0, status VARCHAR(32) NOT NULL,
 expires_at TIMESTAMP NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uk_reservation(order_id,stock_id),
 CONSTRAINT fk_reservation_stock FOREIGN KEY(stock_id) REFERENCES inventory_stock(id),
 CONSTRAINT ck_reservation_quantity CHECK(quantity>0 AND refunded_quantity>=0 AND refunded_quantity<=quantity)
) ENGINE=InnoDB;
-- Terminal RELEASED fences a delayed RESERVE, even when no reservation exists yet.
CREATE TABLE inventory_order_guard (
 order_id VARCHAR(36) PRIMARY KEY, state VARCHAR(24) NOT NULL,
 updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;
-- Inbox + immutable business receipt commit atomically with stock changes.
CREATE TABLE inventory_command_receipt (
 command_id VARCHAR(128) PRIMARY KEY, order_id VARCHAR(36) NOT NULL,
 request_hash CHAR(64) NOT NULL, kind VARCHAR(24) NOT NULL,
 status VARCHAR(24) NOT NULL, response_json JSON NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 INDEX idx_receipt_order(order_id)
) ENGINE=InnoDB;
