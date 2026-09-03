package com.example.locallife.inventory;

import java.time.LocalDateTime;

public record InventoryReservation(
        String id,
        String orderId,
        Long stockId,
        Integer quantity,
        String status,
        LocalDateTime expiresAt
) {
}
