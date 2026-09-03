package com.example.locallife.payment;

import java.time.LocalDateTime;

public record RefundRecord(
        String id,
        String refundNo,
        String paymentId,
        String orderId,
        String userId,
        Long amountMinor,
        String reason,
        String status,
        String providerRefundNo,
        LocalDateTime refundedAt,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}
