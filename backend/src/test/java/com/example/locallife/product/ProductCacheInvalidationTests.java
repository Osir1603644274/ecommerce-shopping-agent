package com.example.locallife.product;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;


import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class ProductCacheInvalidationTests {

    @Test
    void remoteInvalidationEvictsOnlyLocalL1AndNextReadUsesFreshRedisValue() throws Exception {
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        @SuppressWarnings("unchecked")
        ValueOperations<String, String> values = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(values);
        ObjectMapper mapper = new ObjectMapper();
        ProductDetailResponse oldDetail = detail("旧标题", 1L);
        ProductDetailResponse freshDetail = detail("新标题", 2L);
        when(values.get("local-life:product:detail:v1:1001"))
                .thenReturn(mapper.writeValueAsString(oldDetail))
                .thenReturn(mapper.writeValueAsString(freshDetail));
        ProductCache cache = new ProductCache(redis, mapper, 60, 2, 10,
                new SimpleMeterRegistry());

        assertThat(cache.getDetail(1001L).detail().title()).isEqualTo("旧标题");
        cache.invalidateLocal(1001L);

        assertThat(cache.getDetail(1001L).detail().title()).isEqualTo("新标题");
    }

    @Test
    void localDeleteRemovesRedisAndBroadcastsInvalidation() {
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        ProductCache cache = new ProductCache(redis, new ObjectMapper(), 60, 2, 10,
                new SimpleMeterRegistry());

        cache.deleteDetail(1001L);

        verify(redis).delete("local-life:product:detail:v1:1001");
        verify(redis).convertAndSend(ProductCache.INVALIDATION_CHANNEL, "product:1001");
    }

    private static ProductDetailResponse detail(String title, Long entityVersion) {
        return new ProductDetailResponse(
                1001L, "test", "1001", title, "品牌", "商家",
                "手机", null, null, 249900L, "CNY", "verified", "ACTIVE",
                entityVersion, 5, 1L, "", "snapshot", "test-v1", "test",
                "https://example.test/1001", null, java.util.List.of()
        );
    }
}
