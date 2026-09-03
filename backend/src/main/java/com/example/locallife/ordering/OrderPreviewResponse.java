package com.example.locallife.ordering;

public record OrderPreviewResponse(
        String itemType,
        Long itemId,
        String title,
        Long unitPriceMinor,
        Integer quantity,
        Long totalMinor,
        Long discountMinor,
        Long payableMinor,
        String currency,
        String userCouponId,
        Integer availableQuantity,
        String priceEvidence
) {
}
