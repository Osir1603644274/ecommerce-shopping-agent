package com.example.locallife.inventory;

public record InventoryStock(
        Long id,
        String itemType,
        Long itemId,
        Integer totalQuantity,
        Integer availableQuantity,
        Integer reservedQuantity,
        Integer soldQuantity,
        Long version
) {
}
