package com.example.locallife.integration;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.Set;

@Component
class OrderAuditEventHandler implements InboundEventHandler {
    private static final Logger log = LoggerFactory.getLogger(OrderAuditEventHandler.class);
    private static final Set<String> SUPPORTED = Set.of(
            DomainEventTypes.ORDER_CREATED_V1,
            DomainEventTypes.ORDER_PAID_V1,
            DomainEventTypes.ORDER_CANCELLED_V1,
            DomainEventTypes.ORDER_EXPIRED_V1,
            DomainEventTypes.ORDER_REFUNDED_V1
    );

    @Override
    public boolean supports(String eventType) {
        return SUPPORTED.contains(eventType);
    }

    @Override
    public void handle(EventEnvelope event) {
        log.info(
                "订单领域事件已可靠消费: eventId={}, aggregateId={}, type={}",
                event.id(),
                event.aggregateId(),
                event.eventType()
        );
    }
}
