-- Preserve receipt instants instead of rounding a fresh receipt into the future.
ALTER TABLE customer_order MODIFY COLUMN completed_at TIMESTAMP(6) NULL;
