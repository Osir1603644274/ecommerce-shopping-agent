package com.example.locallife.marketing;

import java.time.LocalDateTime;

public record UserCoupon(
        String id,
        Long templateId,
        String userId,
        String status,
        String orderId,
        LocalDateTime claimedAt,
        LocalDateTime usedAt,
        Long version
) {
}
