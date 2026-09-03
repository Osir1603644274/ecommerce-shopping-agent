package com.example.locallife.integration;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.util.UUID;

@Component
@ConditionalOnProperty(prefix = "local-life.messaging", name = "enabled", havingValue = "true")
class OutboxRelay {
    private static final Logger log = LoggerFactory.getLogger(OutboxRelay.class);
    private final OutboxClaimService claimService;
    private final EventTransport transport;
    private final String instanceId = UUID.randomUUID().toString();

    OutboxRelay(OutboxClaimService claimService, EventTransport transport) {
        this.claimService = claimService;
        this.transport = transport;
    }

    @Scheduled(fixedDelayString = "${local-life.messaging.relay-delay:PT1S}")
    void relay() {
        for (String id : claimService.dispatchableIds(100)) {
            claimService.claim(id, instanceId).ifPresent(this::publish);
        }
    }

    void publish(OutboxEvent event) {
        try {
            transport.publish(EventEnvelope.from(event));
            claimService.published(event.id(), instanceId);
        } catch (RuntimeException exception) {
            log.warn(
                    "Outbox 发布失败: transport={}, eventId={}, attempt={}",
                    transport.name(),
                    event.id(),
                    event.attempts() + 1,
                    exception
            );
            claimService.failed(event, instanceId, exception);
        }
    }
}
