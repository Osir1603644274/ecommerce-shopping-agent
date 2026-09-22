ALTER TABLE inventory_reservation ADD COLUMN returned_quantity INT NOT NULL DEFAULT 0;
ALTER TABLE inventory_reservation ADD CONSTRAINT ck_inventory_return_quantity
 CHECK(returned_quantity>=0 AND returned_quantity+refunded_quantity<=quantity);
CREATE TABLE inventory_return_receipt (
 command_id VARCHAR(128) PRIMARY KEY,
 order_id VARCHAR(36) NOT NULL,
 item_id BIGINT NOT NULL,
 stock_id BIGINT NOT NULL,
 quantity INT NOT NULL,
 disposition VARCHAR(32) NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CHECK(quantity>0),
 FOREIGN KEY(stock_id) REFERENCES inventory_stock(id)
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
