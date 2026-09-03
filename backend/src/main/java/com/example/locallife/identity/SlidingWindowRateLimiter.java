package com.example.locallife.identity;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import java.time.Clock;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

@Component
@ConditionalOnProperty(
        name = "local-life.rate-limit.enabled",
        havingValue = "true",
        matchIfMissing = true
)
public class SlidingWindowRateLimiter {
    private static final Logger log = LoggerFactory.getLogger(SlidingWindowRateLimiter.class);
    private static final DefaultRedisScript<Long> ACQUIRE_SCRIPT = new DefaultRedisScript<>(
            """
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
            """,
            Long.class
    );

    private final StringRedisTemplate redis;
    private final Clock clock;
    private final ConcurrentHashMap<String, LocalWindow> localWindows = new ConcurrentHashMap<>();

    @Autowired
    public SlidingWindowRateLimiter(StringRedisTemplate redis) {
        this(redis, Clock.systemUTC());
    }

    public SlidingWindowRateLimiter(StringRedisTemplate redis, Clock clock) {
        this.redis = redis;
        this.clock = clock;
    }

    public Decision acquire(String key, int limit, Duration window) {
        long windowMillis = Math.max(1, window.toMillis());
        int effectiveLimit = Math.max(1, limit);
        try {
            Long result = redis.execute(
                    ACQUIRE_SCRIPT,
                    List.of("rate-limit:sliding:" + key),
                    UUID.randomUUID().toString(),
                    Long.toString(windowMillis),
                    Integer.toString(effectiveLimit)
            );
            if (result == null) {
                throw new IllegalStateException("Redis sliding-window script returned null");
            }
            return result >= 0
                    ? new Decision(true, 0)
                    : new Decision(false, toRetrySeconds(-result));
        } catch (RuntimeException exception) {
            log.warn("Redis rate limit unavailable; using process-local sliding window");
            return acquireLocally(key, effectiveLimit, windowMillis);
        }
    }

    private Decision acquireLocally(String key, int limit, long windowMillis) {
        long nowMillis = clock.millis();
        LocalWindow window = localWindows.computeIfAbsent(key, ignored -> new LocalWindow());
        synchronized (window.timestamps) {
            long cutoff = nowMillis - windowMillis;
            while (!window.timestamps.isEmpty() && window.timestamps.peekFirst() <= cutoff) {
                window.timestamps.removeFirst();
            }
            if (window.timestamps.size() >= limit) {
                long retryMillis = windowMillis - (nowMillis - window.timestamps.peekFirst());
                return new Decision(false, toRetrySeconds(Math.max(1, retryMillis)));
            }
            window.timestamps.addLast(nowMillis);
        }
        if (localWindows.size() > 10_000) {
            localWindows.entrySet().removeIf(entry -> entry.getValue().isExpired(nowMillis, windowMillis));
        }
        return new Decision(true, 0);
    }

    private static long toRetrySeconds(long retryMillis) {
        return Math.max(1, (retryMillis + 999) / 1000);
    }

    public record Decision(boolean allowed, long retryAfterSeconds) {
    }

    private static final class LocalWindow {
        private final ArrayDeque<Long> timestamps = new ArrayDeque<>();

        private boolean isExpired(long nowMillis, long windowMillis) {
            synchronized (timestamps) {
                return timestamps.isEmpty() || timestamps.peekLast() <= nowMillis - windowMillis;
            }
        }
    }
}
