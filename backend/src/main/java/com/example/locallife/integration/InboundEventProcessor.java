package com.example.locallife.integration;

import com.example.locallife.deployment.DeploymentProperties;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
class InboundEventProcessor {
    private final InboxClaimService claims;
    private final List<InboundEventHandler> handlers;
    private final DeploymentProperties deployment;

    InboundEventProcessor(
            InboxClaimService claims,
            List<InboundEventHandler> handlers,
            DeploymentProperties deployment
    ) {
        this.claims = claims;
        this.handlers = handlers;
        this.deployment = deployment;
    }

    void process(String consumer, EventEnvelope event) {
        if (!claims.claim(consumer, event)) {
            return;
        }
        try {
            if (!roleOwns(event.eventType())) {
                claims.processed(consumer, event.id());
                return;
            }
            handlerFor(event.eventType()).handle(event);
            claims.processed(consumer, event.id());
        } catch (RuntimeException exception) {
            if (!claims.failed(consumer, event, exception)) {
                throw exception;
            }
        }
    }

    private boolean roleOwns(String eventType) {
        if ("monolith".equals(deployment.role())) {
            return true;
        }
        boolean catalogEvent = eventType.startsWith("product.search.")
                || eventType.startsWith("shop.") || eventType.startsWith("review.vector.");
        return "catalog".equals(deployment.role()) ? catalogEvent : !catalogEvent;
    }

    private InboundEventHandler handlerFor(String eventType) {
        return handlers.stream()
                .filter(handler -> handler.supports(eventType))
                .findFirst()
                .orElseThrow(() -> new IllegalArgumentException(
                        "没有事件处理器: " + eventType));
    }
}
