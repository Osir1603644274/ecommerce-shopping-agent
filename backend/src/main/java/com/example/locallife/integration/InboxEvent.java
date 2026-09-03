package com.example.locallife.integration;

import java.time.LocalDateTime;

record InboxEvent(
        Long id,
        String consumerName,
        String eventId,
        String eventType,
        String payloadHash,
        String status,
        Integer attempts,
        String lastError,
        LocalDateTime claimedAt,
        LocalDateTime processedAt
) {
}
