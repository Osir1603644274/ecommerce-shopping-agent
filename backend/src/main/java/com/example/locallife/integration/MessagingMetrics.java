package com.example.locallife.integration;

import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Tags;
import jakarta.annotation.PostConstruct;
import org.springframework.stereotype.Component;

@Component
class MessagingMetrics {
    private final MeterRegistry meters;
    private final OutboxMapper outbox;
    private final InboxMapper inbox;

    MessagingMetrics(MeterRegistry meters, OutboxMapper outbox, InboxMapper inbox) {
        this.meters = meters;
        this.outbox = outbox;
        this.inbox = inbox;
    }

    @PostConstruct
    void bind() {
        for (String status : new String[]{"PENDING", "PROCESSING", "PUBLISHED", "DEAD"}) {
            meters.gauge(
                    "local_life.outbox.events",
                    Tags.of("status", status.toLowerCase()),
                    outbox,
                    mapper -> mapper.countByStatus(status)
            );
        }
        for (String status : new String[]{"PROCESSING", "PROCESSED", "FAILED", "DEAD"}) {
            meters.gauge(
                    "local_life.inbox.events",
                    Tags.of("status", status.toLowerCase()),
                    inbox,
                    mapper -> mapper.countByStatus(status)
            );
        }
    }
}
