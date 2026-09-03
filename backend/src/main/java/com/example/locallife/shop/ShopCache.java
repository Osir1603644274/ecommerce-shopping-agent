package com.example.locallife.shop;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.github.benmanes.caffeine.cache.Cache;
import com.github.benmanes.caffeine.cache.Caffeine;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Metrics;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;

@Component
@ConditionalOnProperty(prefix = "local-life.cache.shop", name = "enabled", havingValue = "true", matchIfMissing = true)
public class ShopCache {

    static final String SHOP_DETAIL_KEY_PREFIX = "local-life:shop:detail:";
    static final String SHOP_DETAIL_LOCK_KEY_PREFIX = "local-life:lock:shop:detail:";

    private static final Logger log = LoggerFactory.getLogger(ShopCache.class);
    private static final DefaultRedisScript<Long> UNLOCK_SCRIPT = new DefaultRedisScript<>(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
                return redis.call('del', KEYS[1])
            end
            return 0
            """,
            Long.class
    );

    private final StringRedisTemplate redisTemplate;
    private final ObjectMapper objectMapper;
    private final Duration detailTtl;
    private final Duration emptyDetailTtl;
    private final Duration lockTtl;
    private final Cache<Long, ShopCacheLookup> localCache;
    private final MeterRegistry meters;

    public ShopCache(
            StringRedisTemplate redisTemplate,
            ObjectMapper objectMapper,
            @Value("${local-life.cache.shop.detail-ttl-minutes:30}") long detailTtlMinutes,
            @Value("${local-life.cache.shop.empty-detail-ttl-minutes:2}") long emptyDetailTtlMinutes,
            @Value("${local-life.cache.shop.lock-ttl-seconds:10}") long lockTtlSeconds
    ) {
        this(redisTemplate, objectMapper, detailTtlMinutes, emptyDetailTtlMinutes,
                lockTtlSeconds, Metrics.globalRegistry);
    }

    @Autowired
    public ShopCache(
            StringRedisTemplate redisTemplate,
            ObjectMapper objectMapper,
            @Value("${local-life.cache.shop.detail-ttl-minutes:30}") long detailTtlMinutes,
            @Value("${local-life.cache.shop.empty-detail-ttl-minutes:2}") long emptyDetailTtlMinutes,
            @Value("${local-life.cache.shop.lock-ttl-seconds:10}") long lockTtlSeconds,
            MeterRegistry meters
    ) {
        this.redisTemplate = redisTemplate;
        this.objectMapper = objectMapper;
        this.detailTtl = Duration.ofMinutes(detailTtlMinutes);
        this.emptyDetailTtl = Duration.ofMinutes(emptyDetailTtlMinutes);
        this.lockTtl = Duration.ofSeconds(lockTtlSeconds);
        this.meters = meters;
        this.localCache = Caffeine.newBuilder()
                .maximumSize(10_000)
                .expireAfterWrite(30, TimeUnit.SECONDS)
                .build();
    }

    public ShopCacheLookup getDetail(Long shopId) {
        ShopCacheLookup local = localCache.getIfPresent(shopId);
        if (local != null) {
            record("l1", local.isHit() ? "hit" : "empty");
            return local;
        }
        String key = detailKey(shopId);
        try {
            String json = redisTemplate.opsForValue().get(key);
            if (json == null) {
                record("l2", "miss");
                return ShopCacheLookup.miss();
            }
            if (json.isBlank()) {
                ShopCacheLookup empty = ShopCacheLookup.empty();
                localCache.put(shopId, empty);
                record("l2", "empty");
                return empty;
            }
            ShopCacheLookup hit = ShopCacheLookup.hit(
                    objectMapper.readValue(json, ShopDetailResponse.class)
            );
            localCache.put(shopId, hit);
            record("l2", "hit");
            return hit;
        } catch (JsonProcessingException ex) {
            log.warn("Failed to deserialize shop detail cache, key={}", key, ex);
            deleteDetail(shopId);
            record("l2", "corrupt");
            return ShopCacheLookup.miss();
        } catch (RuntimeException ex) {
            log.warn("Failed to read shop detail cache, key={}", key, ex);
            record("l2", "error");
            return ShopCacheLookup.miss();
        }
    }

    public void putDetail(Long shopId, ShopDetailResponse response) {
        String key = detailKey(shopId);
        try {
            String json = objectMapper.writeValueAsString(response);
            redisTemplate.opsForValue().set(key, json, jitter(detailTtl));
            localCache.put(shopId, ShopCacheLookup.hit(response));
        } catch (JsonProcessingException ex) {
            log.warn("Failed to serialize shop detail cache, key={}", key, ex);
        } catch (RuntimeException ex) {
            log.warn("Failed to write shop detail cache, key={}", key, ex);
        }
    }

    public void putEmptyDetail(Long shopId) {
        String key = detailKey(shopId);
        try {
            redisTemplate.opsForValue().set(key, "", jitter(emptyDetailTtl));
            localCache.put(shopId, ShopCacheLookup.empty());
        } catch (RuntimeException ex) {
            log.warn("Failed to write empty shop detail cache, key={}", key, ex);
        }
    }

    public void deleteDetail(Long shopId) {
        String key = detailKey(shopId);
        localCache.invalidate(shopId);
        try {
            redisTemplate.delete(key);
        } catch (RuntimeException ex) {
            log.warn("Failed to delete shop detail cache, key={}", key, ex);
        }
    }

    public void invalidateLocal(Long shopId) {
        localCache.invalidate(shopId);
    }

    public Optional<String> tryLockDetail(Long shopId) {
        String key = lockKey(shopId);
        String ownerToken = UUID.randomUUID().toString();
        try {
            Boolean locked = redisTemplate.opsForValue().setIfAbsent(key, ownerToken, lockTtl);
            if (!Boolean.TRUE.equals(locked)) {
                meters.counter(
                        "local_life.cache.lock",
                        "entity", "shop",
                        "outcome", "contended"
                ).increment();
            }
            return Boolean.TRUE.equals(locked) ? Optional.of(ownerToken) : Optional.empty();
        } catch (RuntimeException ex) {
            log.warn("Failed to acquire shop detail cache rebuild lock, key={}", key, ex);
            return Optional.of(ownerToken);
        }
    }

    public void unlockDetail(Long shopId, String ownerToken) {
        String key = lockKey(shopId);
        try {
            redisTemplate.execute(UNLOCK_SCRIPT, List.of(key), ownerToken);
        } catch (RuntimeException ex) {
            log.warn("Failed to release shop detail cache rebuild lock, key={}", key, ex);
        }
    }

    private static Duration jitter(Duration base) {
        long baseSeconds = Math.max(1, base.toSeconds());
        long range = Math.max(1, Math.round(baseSeconds * 0.2));
        return Duration.ofSeconds(baseSeconds + ThreadLocalRandom.current().nextLong(-range, range + 1));
    }

    static String detailKey(Long shopId) {
        return SHOP_DETAIL_KEY_PREFIX + shopId;
    }

    static String lockKey(Long shopId) {
        return SHOP_DETAIL_LOCK_KEY_PREFIX + shopId;
    }

    private void record(String tier, String outcome) {
        meters.counter(
                "local_life.cache.lookup",
                "entity", "shop",
                "tier", tier,
                "outcome", outcome
        ).increment();
    }
}
