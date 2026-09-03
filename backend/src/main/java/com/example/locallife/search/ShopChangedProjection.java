package com.example.locallife.search;

import com.example.locallife.integration.DomainEventTypes;
import com.example.locallife.integration.EventEnvelope;
import com.example.locallife.integration.InboundEventHandler;
import com.example.locallife.shop.Shop;
import com.example.locallife.shop.ShopRepository;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Component;

import java.util.Optional;

@Component
class ShopChangedProjection implements InboundEventHandler {
    private final ShopRepository shopRepository;
    private final CacheInvalidationPublisher invalidationPublisher;
    private final Optional<ElasticsearchGateway> search;
    private final ObjectMapper objectMapper;

    ShopChangedProjection(
            ShopRepository shopRepository,
            CacheInvalidationPublisher invalidationPublisher,
            Optional<ElasticsearchGateway> search,
            ObjectMapper objectMapper
    ) {
        this.shopRepository = shopRepository;
        this.invalidationPublisher = invalidationPublisher;
        this.search = search;
        this.objectMapper = objectMapper;
    }

    @Override
    public boolean supports(String eventType) {
        return DomainEventTypes.SHOP_UPDATED_V1.equals(eventType);
    }

    @Override
    public void handle(EventEnvelope event) {
        Long shopId = readShopId(event);
        Shop shop = shopRepository.findById(shopId)
                .orElseThrow(() -> new IllegalStateException(
                        "Updated shop no longer exists: " + shopId));
        invalidationPublisher.shopUpdated(shopId);
        search.ifPresent(gateway -> gateway.indexShop(shop));
    }

    private Long readShopId(EventEnvelope event) {
        try {
            return objectMapper.readTree(event.payloadJson()).path("shopId").longValue();
        } catch (Exception exception) {
            throw new IllegalArgumentException("Shop event payload is invalid", exception);
        }
    }
}
