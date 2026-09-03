package com.example.locallife.integration;

import java.time.LocalDateTime;

public record OutboxEvent(
        String id,
        String aggregateType,
        String aggregateId,
        String eventType,
        String payloadJson,
        String status,
        Integer attempts,
        LocalDateTime nextAttemptAt,
        String lockOwner,
        LocalDateTime lockedAt,
        LocalDateTime publishedAt,
        String lastError,
        LocalDateTime occurredAt
) {
}
