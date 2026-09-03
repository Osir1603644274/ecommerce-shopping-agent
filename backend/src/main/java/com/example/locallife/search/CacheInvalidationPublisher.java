package com.example.locallife.search;

import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

@Component
class CacheInvalidationPublisher {
    static final String CHANNEL = "local-life.cache-invalidation.v1";
    private static final Logger log = LoggerFactory.getLogger(CacheInvalidationPublisher.class);
    private final StringRedisTemplate redisTemplate;
    private final MeterRegistry meters;

    CacheInvalidationPublisher(StringRedisTemplate redisTemplate, MeterRegistry meters) {
        this.redisTemplate = redisTemplate;
        this.meters = meters;
    }

    void shopUpdated(Long shopId) {
        try {
            redisTemplate.convertAndSend(CHANNEL, "shop:" + shopId);
            meters.counter(
                    "local_life.cache.invalidation",
                    "entity", "shop",
                    "outcome", "published"
            ).increment();
        } catch (RuntimeException exception) {
            meters.counter(
                    "local_life.cache.invalidation",
                    "entity", "shop",
                    "outcome", "failure"
            ).increment();
            log.warn("Cache invalidation broadcast failed for shopId={}", shopId, exception);
            throw exception;
        }
    }
}
