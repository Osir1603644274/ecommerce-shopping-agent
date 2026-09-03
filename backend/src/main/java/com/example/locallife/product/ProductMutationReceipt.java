package com.example.locallife.product;

public record ProductMutationReceipt(
        Long productId,
        Long entityVersion,
        String operation,
        String outboxEventId
) {
}
