package com.example.locallife.flashsale;

import java.time.LocalDateTime;

public record FlashSaleCampaign(
        Long id,
        String itemType,
        Long itemId,
        String title,
        Long salePriceMinor,
        Integer totalStock,
        Integer availableStock,
        LocalDateTime startsAt,
        LocalDateTime endsAt,
        String status,
        Long version,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}
