package com.example.locallife.search;

import com.example.locallife.product.ProductCache;
import com.example.locallife.shop.ShopCache;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.util.Optional;

@Component
class CacheInvalidationSubscriber {
    private final Optional<ShopCache> shopCache;
    private final Optional<ProductCache> productCache;
    private final MeterRegistry meters;

    CacheInvalidationSubscriber(
            Optional<ShopCache> shopCache,
            Optional<ProductCache> productCache,
            MeterRegistry meters
    ) {
        this.shopCache = shopCache;
        this.productCache = productCache;
        this.meters = meters;
    }

    void invalidateAll() {
        shopCache.ifPresent(ShopCache::invalidateAllLocal);
        productCache.ifPresent(ProductCache::invalidateAllLocal);
    }

    @org.springframework.context.event.EventListener
    public void reconnected(org.springframework.amqp.rabbit.listener.ConsumeOkEvent event) { invalidateAll(); }

    @org.springframework.context.event.EventListener
    public void disconnected(org.springframework.amqp.rabbit.listener.ListenerContainerConsumerFailedEvent event) { invalidateAll(); }

    public void accept(String value) {
        try {
            String entity;
            if (value.startsWith("shop:")) {
                entity = "shop";
                Long id = Long.valueOf(value.substring("shop:".length()));
                shopCache.ifPresent(cache -> cache.invalidateLocal(id));
            } else if (value.startsWith("product:")) {
                entity = "product";
                Long id = Long.valueOf(value.substring("product:".length()));
                productCache.ifPresent(cache -> cache.invalidateLocal(id));
            } else {
                return;
            }
            meters.counter(
                    "local_life.cache.invalidation",
                    "entity", entity,
                    "outcome", "consumed"
            ).increment();
        } catch (NumberFormatException exception) {
            meters.counter(
                    "local_life.cache.invalidation",
                    "entity", value.startsWith("product:") ? "product" : "shop",
                    "outcome", "malformed"
            ).increment();
        }
    }
}
