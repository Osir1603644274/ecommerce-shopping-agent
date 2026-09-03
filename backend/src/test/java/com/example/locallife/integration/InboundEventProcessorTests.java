package com.example.locallife.integration;

import com.example.locallife.deployment.DeploymentProperties;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class InboundEventProcessorTests {

    @Test
    void catalogRoleHandlesCatalogEvents() {
        InboxClaimService claims = mock(InboxClaimService.class);
        InboundEventHandler handler = handlerFor("product.search.upsert.v1");
        EventEnvelope event = event("product.search.upsert.v1");
        when(claims.claim("catalog-consumer", event)).thenReturn(true);

        processor("catalog", claims, handler).process("catalog-consumer", event);

        verify(handler).handle(event);
        verify(claims).processed("catalog-consumer", event.id());
    }

    @Test
    void catalogRoleAcknowledgesTradeEventsWithoutExecutingThem() {
        InboxClaimService claims = mock(InboxClaimService.class);
        InboundEventHandler handler = handlerFor("order.created.v1");
        EventEnvelope event = event("order.created.v1");
        when(claims.claim("catalog-consumer", event)).thenReturn(true);

        processor("catalog", claims, handler).process("catalog-consumer", event);

        verify(handler, never()).handle(event);
        verify(claims).processed("catalog-consumer", event.id());
    }

    @Test
    void tradeRoleAcknowledgesCatalogEventsWithoutExecutingThem() {
        InboxClaimService claims = mock(InboxClaimService.class);
        InboundEventHandler handler = handlerFor("product.search.delete.v1");
        EventEnvelope event = event("product.search.delete.v1");
        when(claims.claim("trade-consumer", event)).thenReturn(true);

        processor("trade", claims, handler).process("trade-consumer", event);

        verify(handler, never()).handle(event);
        verify(claims).processed("trade-consumer", event.id());
    }

    @Test
    void tradeRoleHandlesTradeEvents() {
        InboxClaimService claims = mock(InboxClaimService.class);
        InboundEventHandler handler = handlerFor("order.expired.v1");
        EventEnvelope event = event("order.expired.v1");
        when(claims.claim("trade-consumer", event)).thenReturn(true);

        processor("trade", claims, handler).process("trade-consumer", event);

        verify(handler).handle(event);
        verify(claims).processed("trade-consumer", event.id());
    }

    private static InboundEventProcessor processor(
            String role,
            InboxClaimService claims,
            InboundEventHandler handler
    ) {
        return new InboundEventProcessor(
                claims,
                List.of(handler),
                new DeploymentProperties(role, false, "")
        );
    }

    private static InboundEventHandler handlerFor(String eventType) {
        InboundEventHandler handler = mock(InboundEventHandler.class);
        when(handler.supports(eventType)).thenReturn(true);
        return handler;
    }

    private static EventEnvelope event(String eventType) {
        return new EventEnvelope(
                "event-1",
                "TEST",
                "aggregate-1",
                eventType,
                "{}",
                LocalDateTime.of(2026, 9, 1, 0, 0)
        );
    }
}
