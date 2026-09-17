-- Explicit local-demo offers, separate from historical dataset snapshot prices.
CREATE TABLE product_local_offer (
    product_id BIGINT PRIMARY KEY,
    price_minor BIGINT NOT NULL,
    currency VARCHAR(16) NOT NULL DEFAULT 'CNY',
    price_kind VARCHAR(32) NOT NULL DEFAULT 'local_simulated',
    source_revision VARCHAR(128) NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_local_offer_product FOREIGN KEY (product_id) REFERENCES product(id),
    CONSTRAINT ck_local_offer_price CHECK (price_minor > 0),
    CONSTRAINT ck_local_offer_kind CHECK (price_kind = 'local_simulated')
);
