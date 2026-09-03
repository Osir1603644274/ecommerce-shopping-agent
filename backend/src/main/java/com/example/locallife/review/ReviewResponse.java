package com.example.locallife.review;

import java.util.List;

public record ReviewResponse(
        String reviewId,
        Long shopId,
        String shopName,
        String content,
        String source,
        String language,
        String contentZh,
        String translationStatus,
        String sourceReviewId,
        String sourceUserId,
        Double stars,
        String sourceShopName,
        String evidenceScope,
        List<String> tags
) {
    public ReviewResponse(
            String reviewId,
            Long shopId,
            String shopName,
            String content,
            String source,
            String language,
            String contentZh,
            String translationStatus,
            List<String> tags
    ) {
        this(
                reviewId, shopId, shopName, content, source, language, contentZh,
                translationStatus, null, null, null, null, null, tags
        );
    }
}
