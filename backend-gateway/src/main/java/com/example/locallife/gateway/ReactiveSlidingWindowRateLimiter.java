package com.example.locallife.gateway;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.data.redis.core.ReactiveStringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Mono;

import java.time.Clock;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

@Component
@ConditionalOnProperty(name = "gateway.rate-limit.enabled", havingValue = "true")
class ReactiveSlidingWindowRateLimiter {
    private static final Logger log = LoggerFactory.getLogger(ReactiveSlidingWindowRateLimiter.class);
    private static final DefaultRedisScript<Long> ACQUIRE_SCRIPT = new DefaultRedisScript<>("""
            local serverTime = redis.call('TIME')
            local nowMillis = serverTime[1] * 1000 + math.floor(serverTime[2] / 1000)
            local windowMillis = tonumber(ARGV[2])
            local limit = tonumber(ARGV[3])
            redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', nowMillis - windowMillis)
            local count = redis.call('ZCARD', KEYS[1])
            if count >= limit then
                local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
                local retryMillis = windowMillis
                if oldest[2] then
                    retryMillis = math.max(1, windowMillis - (nowMillis - tonumber(oldest[2])))
                end
                return -retryMillis
            end
            redis.call('ZADD', KEYS[1], nowMillis, ARGV[1])
            redis.call('PEXPIRE', KEYS[1], windowMillis)
            return 0
            """, Long.class);

    private final ReactiveStringRedisTemplate redis;
    private final Clock clock;
    private final ConcurrentHashMap<String, LocalWindow> localWindows = new ConcurrentHashMap<>();

    @Autowired
    ReactiveSlidingWindowRateLimiter(ReactiveStringRedisTemplate redis) {
        this(redis, Clock.systemUTC());
    }

    ReactiveSlidingWindowRateLimiter(ReactiveStringRedisTemplate redis, Clock clock) {
        this.redis = redis;
        this.clock = clock;
    }

    Mono<Decision> acquire(String dimension, int limit, Duration window) {
        long windowMillis = Math.max(1, window.toMillis());
        int effectiveLimit = Math.max(1, limit);
        return redis.execute(
                        ACQUIRE_SCRIPT,
                        List.of("gateway:rate-limit:" + dimension),
                        List.of(UUID.randomUUID().toString(),
                                Long.toString(windowMillis), Integer.toString(effectiveLimit)))
                .single()
                .map(result -> result >= 0
                        ? new Decision(true, 0)
                        : new Decision(false, retrySeconds(-result)))
                .switchIfEmpty(Mono.error(new IllegalStateException("empty Redis script result")))
                .onErrorResume(exception -> {
                    log.warn("Gateway Redis rate limit unavailable; using process-local window");
                    return Mono.just(acquireLocally(dimension, effectiveLimit, windowMillis));
                });
    }

    private Decision acquireLocally(String key, int limit, long windowMillis) {
        long now = clock.millis();
        LocalWindow window = localWindows.computeIfAbsent(key, ignored -> new LocalWindow());
        synchronized (window.timestamps) {
            long cutoff = now - windowMillis;
            while (!window.timestamps.isEmpty() && window.timestamps.peekFirst() <= cutoff) {
                window.timestamps.removeFirst();
            }
            if (window.timestamps.size() >= limit) {
                return new Decision(false, retrySeconds(
                        windowMillis - (now - window.timestamps.peekFirst())));
            }
            window.timestamps.addLast(now);
            return new Decision(true, 0);
        }
    }

    private static long retrySeconds(long millis) {
        return Math.max(1, (Math.max(1, millis) + 999) / 1000);
    }

    record Decision(boolean allowed, long retryAfterSeconds) {
    }

    private static final class LocalWindow {
        private final ArrayDeque<Long> timestamps = new ArrayDeque<>();
    }
}
