package com.example.locallife.behavior;

import java.time.LocalDateTime;

public record UserBehaviorResponse(
        String id,
        String userId,
        Long shopId,
        String behaviorType,
        Double score,
        String source,
        String sourceUserId,
        String sourceReviewId,
        LocalDateTime occurredAt
) {
}
