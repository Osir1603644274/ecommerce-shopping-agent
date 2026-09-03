package com.example.locallife.shop;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.data.redis.core.script.DefaultRedisScript;

import java.time.Duration;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class ShopCacheTests {

    private final StringRedisTemplate redisTemplate = mock(StringRedisTemplate.class);
    private final ValueOperations<String, String> valueOperations = mock(ValueOperations.class);
    private final ObjectMapper objectMapper = new ObjectMapper();
    private ShopCache shopCache;

    @BeforeEach
    void setUp() {
        when(redisTemplate.opsForValue()).thenReturn(valueOperations);
        shopCache = new ShopCache(redisTemplate, objectMapper, 30, 2, 10);
    }

    @Test
    void getDetailReturnsCachedShopDetail() {
        String key = ShopCache.detailKey(3L);
        when(valueOperations.get(key)).thenReturn("""
                {"id":3,"name":"清晨手冲咖啡","typeId":2,"address":"文化广场 3 号","avgPrice":35,"phone":"010-8888-0003"}
                """);

        ShopCacheLookup result = shopCache.getDetail(3L);

        assertThat(result.status()).isEqualTo(ShopCacheLookup.Status.HIT);
        assertThat(result.detail()).isEqualTo(new ShopDetailResponse(
                3L, "清晨手冲咖啡", 2L, "文化广场 3 号", 35, "010-8888-0003"));
        verify(valueOperations).get(key);
    }

    @Test
    void getDetailReturnsEmptyWhenCachedValueIsBlank() {
        String key = ShopCache.detailKey(999L);
        when(valueOperations.get(key)).thenReturn("");

        ShopCacheLookup result = shopCache.getDetail(999L);

        assertThat(result.status()).isEqualTo(ShopCacheLookup.Status.EMPTY);
        assertThat(result.detail()).isNull();
        verify(valueOperations).get(key);
    }

    @Test
    void getDetailReturnsMissWhenCacheKeyDoesNotExist() {
        String key = ShopCache.detailKey(999L);
        when(valueOperations.get(key)).thenReturn(null);

        ShopCacheLookup result = shopCache.getDetail(999L);

        assertThat(result.status()).isEqualTo(ShopCacheLookup.Status.MISS);
        assertThat(result.detail()).isNull();
        verify(valueOperations).get(key);
    }

    @Test
    void putDetailWritesJsonWithTtl() throws Exception {
        ShopDetailResponse response = new ShopDetailResponse(
                3L, "清晨手冲咖啡", 2L, "文化广场 3 号", 35, "010-8888-0003");
        ArgumentCaptor<String> jsonCaptor = ArgumentCaptor.forClass(String.class);
        ArgumentCaptor<Duration> ttlCaptor = ArgumentCaptor.forClass(Duration.class);

        shopCache.putDetail(3L, response);

        verify(valueOperations).set(
                eq(ShopCache.detailKey(3L)),
                jsonCaptor.capture(),
                ttlCaptor.capture()
        );
        assertThat(ttlCaptor.getValue()).isBetween(Duration.ofMinutes(24), Duration.ofMinutes(36));
        ShopDetailResponse cached = objectMapper.readValue(jsonCaptor.getValue(), ShopDetailResponse.class);
        assertThat(cached).isEqualTo(response);
    }

    @Test
    void putEmptyDetailWritesBlankValueWithShortTtl() {
        ArgumentCaptor<Duration> ttlCaptor = ArgumentCaptor.forClass(Duration.class);
        shopCache.putEmptyDetail(999L);

        verify(valueOperations).set(
                eq(ShopCache.detailKey(999L)),
                eq(""),
                ttlCaptor.capture()
        );
        assertThat(ttlCaptor.getValue()).isBetween(Duration.ofSeconds(96), Duration.ofSeconds(144));
    }

    @Test
    void deleteDetailDeletesShopDetailKey() {
        shopCache.deleteDetail(3L);

        verify(redisTemplate).delete(ShopCache.detailKey(3L));
    }

    @Test
    void tryLockDetailReturnsTrueWhenRedisLockIsAcquired() {
        when(valueOperations.setIfAbsent(
                eq(ShopCache.lockKey(3L)),
                anyString(),
                eq(Duration.ofSeconds(10))
        ))
                .thenReturn(true);

        Optional<String> lockToken = shopCache.tryLockDetail(3L);

        assertThat(lockToken).isPresent();
        verify(valueOperations).setIfAbsent(
                eq(ShopCache.lockKey(3L)),
                eq(lockToken.orElseThrow()),
                eq(Duration.ofSeconds(10))
        );
    }

    @Test
    void tryLockDetailReturnsFalseWhenRedisLockAlreadyExists() {
        when(valueOperations.setIfAbsent(
                eq(ShopCache.lockKey(3L)),
                anyString(),
                eq(Duration.ofSeconds(10))
        ))
                .thenReturn(false);

        Optional<String> lockToken = shopCache.tryLockDetail(3L);

        assertThat(lockToken).isEmpty();
    }

    @Test
    void unlockDetailDeletesOnlyLockOwnedByCaller() {
        shopCache.unlockDetail(3L, "owner-token");

        verify(redisTemplate).execute(
                any(DefaultRedisScript.class),
                eq(List.of(ShopCache.lockKey(3L))),
                eq("owner-token")
        );
    }
}
