package com.example.locallife.product;

/** MySQL authority snapshot used after ES recall and before recommendation. */
public record ProductCommerceFacts(
        Long productId,
        Long snapshotPriceMinor,
        String priceStatus,
        String lifecycleStatus,
        Long entityVersion,
        Integer availableQuantity,
        Long inventoryVersion
) {
}
