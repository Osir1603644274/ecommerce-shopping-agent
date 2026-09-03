package com.example.locallife.search;

import com.example.locallife.integration.DomainEventTypes;
import com.example.locallife.integration.EventEnvelope;
import com.example.locallife.integration.InboundEventHandler;
import com.example.locallife.integration.ProductSearchEventPayload;
import com.example.locallife.product.Product;
import com.example.locallife.product.ProductRepository;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.Optional;

@Component
class ProductChangedProjection implements InboundEventHandler {
    private final ProductRepository products;
    private final ProductSearchProjectionCursorMapper cursor;
    private final Optional<ElasticsearchGateway> search;
    private final ObjectMapper json;
    private final MeterRegistry meters;

    ProductChangedProjection(
            ProductRepository products,
            ProductSearchProjectionCursorMapper cursor,
            Optional<ElasticsearchGateway> search,
            ObjectMapper json,
            MeterRegistry meters
    ) {
        this.products = products;
        this.cursor = cursor;
        this.search = search;
        this.json = json;
        this.meters = meters;
    }

    @Override
    public boolean supports(String eventType) {
        return DomainEventTypes.PRODUCT_SEARCH_UPSERT_V1.equals(eventType)
                || DomainEventTypes.PRODUCT_SEARCH_DELETE_V1.equals(eventType);
    }

    @Override
    @Transactional
    public void handle(EventEnvelope event) {
        ProductSearchEventPayload payload = read(event);
        validateIdentity(event, payload);
        Long applied = cursor.findVersionForUpdate(payload.productId());
        if (applied != null && payload.entityVersion() <= applied) {
            meters.counter("local_life.search.product_event", "outcome", "stale_or_duplicate").increment();
            return;
        }
        ElasticsearchGateway gateway = search.orElseThrow(() ->
                new IllegalStateException("Elasticsearch is disabled for product projection"));
        Product product = products.findById(payload.productId()).orElseThrow(() ->
                new IllegalStateException("Product mutation is missing from MySQL"));
        if (!payload.entityVersion().equals(product.entityVersion())) {
            throw new IllegalStateException("Product event version does not match MySQL authority");
        }
        if ("DELETE".equals(payload.operation())) {
            if (!"DELETED".equalsIgnoreCase(product.lifecycleStatus())) {
                throw new IllegalStateException("Delete event requires DELETED MySQL lifecycle status");
            }
            gateway.deleteProduct(product.id(), product.entityVersion());
        } else {
            if (!"ACTIVE".equalsIgnoreCase(product.lifecycleStatus())) {
                throw new IllegalStateException("Upsert event requires ACTIVE MySQL lifecycle status");
            }
            gateway.indexProduct(product, product.entityVersion());
        }
        cursor.advance(product.id(), product.entityVersion(), event.id(), payload.operation());
        long lagMs = Math.max(0L, Duration.between(event.occurredAt(), LocalDateTime.now()).toMillis());
        meters.summary("local_life.search.product_index_lag_ms").record(lagMs);
        meters.counter("local_life.search.product_event", "outcome", "applied").increment();
    }

    private ProductSearchEventPayload read(EventEnvelope event) {
        try {
            return json.readValue(event.payloadJson(), ProductSearchEventPayload.class);
        } catch (Exception exception) {
            throw new IllegalArgumentException("Product search event payload is invalid", exception);
        }
    }

    private static void validateIdentity(EventEnvelope event, ProductSearchEventPayload payload) {
        if (!"PRODUCT".equals(event.aggregateType())
                || !event.aggregateId().equals(Long.toString(payload.productId()))
                || payload.entityVersion() == null || payload.entityVersion() <= 0
                || payload.idempotencyKey() == null
                || !payload.idempotencyKey().equals(
                        "product:" + payload.productId() + ":v" + payload.entityVersion() + ":" + payload.operation())) {
            throw new IllegalArgumentException("Product search event identity binding is invalid");
        }
        boolean upsert = DomainEventTypes.PRODUCT_SEARCH_UPSERT_V1.equals(event.eventType())
                && "UPSERT".equals(payload.operation());
        boolean delete = DomainEventTypes.PRODUCT_SEARCH_DELETE_V1.equals(event.eventType())
                && "DELETE".equals(payload.operation());
        if (!upsert && !delete) {
            throw new IllegalArgumentException("Product search event type/operation mismatch");
        }
    }
}
