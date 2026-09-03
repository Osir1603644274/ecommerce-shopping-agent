package com.example.locallife.flashsale;

import java.time.LocalDateTime;

public record FlashSaleOrder(
        String id,
        Long campaignId,
        String userId,
        String status,
        Long amountMinor,
        String streamMessageId,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}
