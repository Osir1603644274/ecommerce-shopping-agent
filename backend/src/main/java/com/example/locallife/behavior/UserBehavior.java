package com.example.locallife.behavior;

import java.time.LocalDateTime;

public record UserBehavior(
        String id,
        String userId,
        Long shopId,
        String behaviorType,
        Double score,
        String source,
        String sourceUserId,
        String sourceReviewId,
        LocalDateTime occurredAt,
        LocalDateTime createdAt
) {
    public UserBehavior(
            String id,
            String userId,
            Long shopId,
            String behaviorType,
            Double score,
            String source,
            LocalDateTime occurredAt,
            LocalDateTime createdAt
    ) {
        this(
                id, userId, shopId, behaviorType, score, source,
                null, null, occurredAt, createdAt
        );
    }
}
