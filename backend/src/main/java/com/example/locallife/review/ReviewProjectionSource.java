package com.example.locallife.review;

import com.example.locallife.review.Review;
import com.example.locallife.review.ReviewRepository;
import com.example.locallife.shop.ShopRepository;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

/** Events trigger a fresh authoritative snapshot rather than replaying old review content. */
@Service
class ReviewProjectionSource {
    private final JdbcTemplate jdbc;
    private final ReviewRepository reviews;
    private final ShopRepository shops;

    ReviewProjectionSource(JdbcTemplate jdbc, ReviewRepository reviews, ShopRepository shops) {
        this.jdbc=jdbc; this.reviews=reviews; this.shops=shops;
    }

    @Transactional(propagation=Propagation.REQUIRES_NEW, isolation=Isolation.READ_COMMITTED)
    public Snapshot snapshot(String reviewId) {
        jdbc.update("""
                INSERT INTO review_projection_head(review_id,revision) VALUES(?,0)
                ON DUPLICATE KEY UPDATE review_id=VALUES(review_id)
                """, reviewId);
        long revision = jdbc.queryForObject(
                "SELECT revision FROM review_projection_head WHERE review_id=? FOR UPDATE",Long.class,reviewId)+1;
        Review review = reviews.findById(reviewId).orElse(null);
        String shopName = review == null ? null : shops.findById(review.shopId())
                .orElseThrow(() -> new IllegalStateException("Review shop no longer exists")).name();
        jdbc.update("UPDATE review_projection_head SET revision=? WHERE review_id=?",revision,reviewId);
        return new Snapshot(revision, review, shopName);
    }

    record Snapshot(long revision, Review review, String shopName) { }
}
