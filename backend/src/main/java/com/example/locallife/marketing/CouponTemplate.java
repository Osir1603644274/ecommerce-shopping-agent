package com.example.locallife.marketing;

import java.time.LocalDateTime;

public record CouponTemplate(
        Long id,
        String name,
        Long thresholdMinor,
        Long discountMinor,
        Integer totalQuantity,
        Integer claimedQuantity,
        LocalDateTime validFrom,
        LocalDateTime validUntil,
        String status,
        Long version
) {
}
