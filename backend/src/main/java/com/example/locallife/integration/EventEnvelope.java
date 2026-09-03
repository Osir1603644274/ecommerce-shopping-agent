package com.example.locallife.integration;

import java.time.LocalDateTime;

public record EventEnvelope(
        String id,
        String aggregateType,
        String aggregateId,
        String eventType,
        String payloadJson,
        LocalDateTime occurredAt
) {
    static EventEnvelope from(OutboxEvent event) {
        return new EventEnvelope(
                event.id(),
                event.aggregateType(),
                event.aggregateId(),
                event.eventType(),
                event.payloadJson(),
                event.occurredAt()
        );
    }
}
