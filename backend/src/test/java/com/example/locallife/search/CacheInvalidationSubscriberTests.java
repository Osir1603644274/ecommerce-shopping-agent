package com.example.locallife.search;

import com.example.locallife.product.ProductCache;
import com.example.locallife.shop.ShopCache;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.connection.DefaultMessage;

import java.nio.charset.StandardCharsets;
import java.util.Optional;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;

class CacheInvalidationSubscriberTests {
    @Test
    void invalidatesOnlyTheLocalL1EntryForBroadcastShopMessage() {
        ShopCache cache = mock(ShopCache.class);
        CacheInvalidationSubscriber subscriber = new CacheInvalidationSubscriber(
                Optional.of(cache),
                Optional.empty(),
                new SimpleMeterRegistry()
        );

        subscriber.accept("shop:17");

        verify(cache).invalidateLocal(17L);
    }

    @Test
    void ignoresUnknownMessageTypes() {
        ShopCache cache = mock(ShopCache.class);
        CacheInvalidationSubscriber subscriber = new CacheInvalidationSubscriber(
                Optional.of(cache),
                Optional.empty(),
                new SimpleMeterRegistry()
        );

        subscriber.accept("product:17");

        verifyNoInteractions(cache);
    }

    @Test
    void invalidatesOnlyTheLocalL1EntryForBroadcastProductMessage() {
        ProductCache cache = mock(ProductCache.class);
        CacheInvalidationSubscriber subscriber = new CacheInvalidationSubscriber(
                Optional.empty(),
                Optional.of(cache),
                new SimpleMeterRegistry()
        );

        subscriber.accept("product:23");

        verify(cache).invalidateLocal(23L);
    }

    private static DefaultMessage message(String value) {
        return new DefaultMessage(
                CacheInvalidationPublisher.CHANNEL.getBytes(StandardCharsets.UTF_8),
                value.getBytes(StandardCharsets.UTF_8)
        );
    }
}
