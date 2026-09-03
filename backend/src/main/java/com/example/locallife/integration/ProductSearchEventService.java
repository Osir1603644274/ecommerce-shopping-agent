package com.example.locallife.integration;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

/** Must be called inside the same MySQL transaction as the product mutation. */
@Service
public class ProductSearchEventService {
    private final OutboxService outbox;

    public ProductSearchEventService(OutboxService outbox) {
        this.outbox = outbox;
    }

    @Transactional(propagation = Propagation.MANDATORY)
    public String productUpserted(long productId, long entityVersion) {
        return append(productId, entityVersion, "UPSERT", DomainEventTypes.PRODUCT_SEARCH_UPSERT_V1);
    }

    @Transactional(propagation = Propagation.MANDATORY)
    public String productDeleted(long productId, long entityVersion) {
        return append(productId, entityVersion, "DELETE", DomainEventTypes.PRODUCT_SEARCH_DELETE_V1);
    }

    private String append(long productId, long entityVersion, String operation, String eventType) {
        if (productId <= 0 || entityVersion <= 0) {
            throw new IllegalArgumentException("productId/entityVersion must be positive");
        }
        String key = "product:" + productId + ":v" + entityVersion + ":" + operation;
        return outbox.appendIdempotent(
                key, "PRODUCT", Long.toString(productId), eventType,
                new ProductSearchEventPayload(productId, entityVersion, operation, key)
        );
    }
}
