package com.example.locallife.ordering;

import java.time.LocalDateTime;

public record CustomerOrder(
        String id,
        String orderNo,
        String userId,
        String idempotencyKey,
        String requestHash,
        String status,
        Long totalMinor,
        Long discountMinor,
        Long payableMinor,
        String currency,
        String userCouponId,
        LocalDateTime expiresAt,
        LocalDateTime paidAt,
        LocalDateTime completedAt,
        LocalDateTime cancelledAt,
        Long version,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}
