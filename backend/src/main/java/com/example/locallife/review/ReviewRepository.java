package com.example.locallife.review;

import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

@Repository
public class ReviewRepository {

    private final ReviewMapper mapper;

    public ReviewRepository(ReviewMapper mapper) {
        this.mapper = mapper;
    }

    public List<Review> findAll() {
        return mapper.findAll();
    }

    public List<Review> findByShopId(Long shopId) {
        return mapper.findByShopId(shopId);
    }

    public void insert(
            String reviewId,
            Long shopId,
            String ownerUserId,
            String content,
            String tags
    ) {
        mapper.insert(reviewId, shopId, ownerUserId, content, tags);
    }

    public Optional<Review> findById(String reviewId) {
        return Optional.ofNullable(mapper.findById(reviewId));
    }

    public void update(String reviewId, String content, String tags) {
        mapper.update(reviewId, content, tags);
    }

    public int deleteById(String reviewId) {
        return mapper.deleteById(reviewId);
    }
}
