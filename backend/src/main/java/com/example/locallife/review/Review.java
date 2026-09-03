package com.example.locallife.review;

import java.time.LocalDateTime;

public record Review(
        String id,
        Long shopId,
        String ownerUserId,
        String content,
        String tags,
        String source,
        String language,
        String contentZh,
        String translationStatus,
        String sourceReviewId,
        String sourceUserId,
        Double stars,
        String sourceShopName,
        String evidenceScope,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}
