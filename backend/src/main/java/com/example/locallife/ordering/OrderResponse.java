package com.example.locallife.ordering;

import java.time.LocalDateTime;
import java.util.List;

public record OrderResponse(
        String id,
        String orderNo,
        String status,
        Long totalMinor,
        Long discountMinor,
        Long payableMinor,
        String currency,
        String userCouponId,
        LocalDateTime expiresAt,
        LocalDateTime paidAt,
        LocalDateTime createdAt,
        List<OrderItem> items
) {
}
