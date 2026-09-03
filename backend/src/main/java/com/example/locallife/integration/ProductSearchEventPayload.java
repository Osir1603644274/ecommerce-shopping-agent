package com.example.locallife.integration;

public record ProductSearchEventPayload(
        Long productId,
        Long entityVersion,
        String operation,
        String idempotencyKey
) {
}
