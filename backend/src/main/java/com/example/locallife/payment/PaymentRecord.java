package com.example.locallife.payment;

import java.time.LocalDateTime;

public record PaymentRecord(
        String id,
        String paymentNo,
        String orderId,
        String userId,
        String provider,
        String status,
        Long amountMinor,
        String currency,
        String providerTradeNo,
        LocalDateTime paidAt,
        Long version,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}
