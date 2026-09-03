package com.example.locallife.ordering;

public record OrderItem(
        Long id,
        String orderId,
        String itemType,
        Long itemId,
        String titleSnapshot,
        Long unitPriceMinor,
        Integer quantity,
        Long subtotalMinor,
        String evidenceJson
) {
}
