package com.example.locallife.product;

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
@ConditionalOnProperty(prefix = "local-life.cache.product", name = "enabled", havingValue = "true", matchIfMissing = true)
public class ProductCache {
    private static final Logger log = LoggerFactory.getLogger(ProductCache.class);
    public static final String INVALIDATION_CHANNEL = "local-life.cache-invalidation.v1";
    private static final String KEY_PREFIX = "local-life:product:detail:v1:";
    private static final String LOCK_KEY_PREFIX = "local-life:lock:product:detail:";
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
    private final Duration emptyTtl;
    private final Duration lockTtl;
    private final Cache<Long, ProductCacheLookup> localCache;
    private final MeterRegistry meters;

    public ProductCache(
            StringRedisTemplate redisTemplate,
            ObjectMapper objectMapper,
            @Value("${local-life.cache.product.detail-ttl-minutes:60}") long detailTtlMinutes,
            @Value("${local-life.cache.product.empty-detail-ttl-minutes:2}") long emptyTtlMinutes,
            @Value("${local-life.cache.product.lock-ttl-seconds:10}") long lockTtlSeconds
    ) {
        this(redisTemplate, objectMapper, detailTtlMinutes, emptyTtlMinutes,
                lockTtlSeconds, Metrics.globalRegistry);
    }

    @Autowired
    public ProductCache(
            StringRedisTemplate redisTemplate,
            ObjectMapper objectMapper,
            @Value("${local-life.cache.product.detail-ttl-minutes:60}") long detailTtlMinutes,
            @Value("${local-life.cache.product.empty-detail-ttl-minutes:2}") long emptyTtlMinutes,
            @Value("${local-life.cache.product.lock-ttl-seconds:10}") long lockTtlSeconds,
            MeterRegistry meters
    ) {
        this.redisTemplate = redisTemplate;
        this.objectMapper = objectMapper;
        this.detailTtl = Duration.ofMinutes(detailTtlMinutes);
        this.emptyTtl = Duration.ofMinutes(emptyTtlMinutes);
        this.lockTtl = Duration.ofSeconds(lockTtlSeconds);
        this.meters = meters;
        this.localCache = Caffeine.newBuilder()
                .maximumSize(10_000)
                .expireAfterWrite(30, TimeUnit.SECONDS)
                .build();
    }

    public ProductCacheLookup getDetail(Long id) {
        ProductCacheLookup local = localCache.getIfPresent(id);
        if (local != null) {
            record("l1", local.isHit() ? "hit" : "empty");
            return local;
        }
        try {
            String json = redisTemplate.opsForValue().get(key(id));
            if (json == null) {
                record("l2", "miss");
                return ProductCacheLookup.miss();
            }
            if (json.isBlank()) {
                ProductCacheLookup empty = ProductCacheLookup.empty();
                localCache.put(id, empty);
                record("l2", "empty");
                return empty;
            }
            ProductCacheLookup hit = ProductCacheLookup.hit(
                    objectMapper.readValue(json, ProductDetailResponse.class)
            );
            localCache.put(id, hit);
            record("l2", "hit");
            return hit;
        } catch (JsonProcessingException ex) {
            deleteDetail(id);
            record("l2", "corrupt");
            return ProductCacheLookup.miss();
        } catch (RuntimeException ex) {
            log.warn("Failed to read product cache for id={}", id, ex);
            record("l2", "error");
            return ProductCacheLookup.miss();
        }
    }

    public void putDetail(Long id, ProductDetailResponse detail) {
        try {
            redisTemplate.opsForValue().set(
                    key(id),
                    objectMapper.writeValueAsString(detail),
                    jitter(detailTtl)
            );
            localCache.put(id, ProductCacheLookup.hit(detail));
        } catch (JsonProcessingException | RuntimeException ex) {
            log.warn("Failed to write product cache for id={}", id, ex);
        }
    }

    public void putEmpty(Long id) {
        try {
            redisTemplate.opsForValue().set(key(id), "", jitter(emptyTtl));
            localCache.put(id, ProductCacheLookup.empty());
        } catch (RuntimeException ex) {
            log.warn("Failed to write empty product cache for id={}", id, ex);
        }
    }

    public void deleteDetail(Long id) {
        localCache.invalidate(id);
        try {
            redisTemplate.delete(key(id));
        } catch (RuntimeException ex) {
            log.warn("Failed to delete product cache for id={}", id, ex);
        }
    }

    public void invalidateLocal(Long id) {
        localCache.invalidate(id);
    }

    public void invalidateAllLocal() { localCache.invalidateAll(); }

    public Optional<String> tryLockDetail(Long id) {
        String ownerToken = UUID.randomUUID().toString();
        try {
            Boolean locked = redisTemplate.opsForValue()
                    .setIfAbsent(lockKey(id), ownerToken, lockTtl);
            if (!Boolean.TRUE.equals(locked)) {
                meters.counter(
                        "local_life.cache.lock",
                        "entity", "product",
                        "outcome", "contended"
                ).increment();
            }
            return Boolean.TRUE.equals(locked) ? Optional.of(ownerToken) : Optional.empty();
        } catch (RuntimeException exception) {
            log.warn("Failed to acquire product cache rebuild lock for id={}", id, exception);
            return Optional.of(ownerToken);
        }
    }

    public void unlockDetail(Long id, String ownerToken) {
        try {
            redisTemplate.execute(UNLOCK_SCRIPT, List.of(lockKey(id)), ownerToken);
        } catch (RuntimeException exception) {
            log.warn("Failed to release product cache rebuild lock for id={}", id, exception);
        }
    }

    private static Duration jitter(Duration base) {
        long baseSeconds = Math.max(1, base.toSeconds());
        long range = Math.max(1, Math.round(baseSeconds * 0.2));
        return Duration.ofSeconds(baseSeconds + ThreadLocalRandom.current().nextLong(-range, range + 1));
    }

    public static String key(Long id) {
        return KEY_PREFIX + id;
    }

    private static String lockKey(Long id) {
        return LOCK_KEY_PREFIX + id;
    }

    private void record(String tier, String outcome) {
        com.example.locallife.diagnostics.BackendTrace.mark("cache", "商品详情 " + tier, outcome);
        meters.counter(
                "local_life.cache.lookup",
                "entity", "product",
                "tier", tier,
                "outcome", outcome
        ).increment();
    }
}
