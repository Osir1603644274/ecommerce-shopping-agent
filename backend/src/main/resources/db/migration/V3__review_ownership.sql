ALTER TABLE review
    ADD COLUMN owner_user_id VARCHAR(36) NULL AFTER shop_id,
    ADD INDEX idx_review_owner_user_id (owner_user_id),
    ADD CONSTRAINT fk_review_owner_user
        FOREIGN KEY (owner_user_id) REFERENCES user_account(id) ON DELETE SET NULL;
