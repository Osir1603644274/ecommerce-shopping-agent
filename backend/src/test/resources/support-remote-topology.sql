-- Fresh isolated trade schema only. Mirrors topology_lab.py's deployed remote
-- inventory boundary: stock_id identifies the OTHER service's row, not a local FK.
-- Keep owner/order/payment constraints and all local-inventory tests unchanged.
ALTER TABLE order_line_allocation DROP FOREIGN KEY fk_line_allocation_stock;
