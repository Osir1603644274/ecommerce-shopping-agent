package com.example.locallife.identity;

import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.ZoneId;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class SlidingWindowRateLimiterTests {
    private final StringRedisTemplate redis = mock(StringRedisTemplate.class);

    @Test
    void redisSlidingWindowAllowsWithinLimitAndReturnsPreciseRetryForOverflow() {
        when(redis.execute(
                any(DefaultRedisScript.class),
                anyList(),
                any(Object[].class)
        )).thenReturn(0L, -1500L);
        SlidingWindowRateLimiter limiter = new SlidingWindowRateLimiter(redis);

        assertThat(limiter.acquire("auth-ip:127.0.0.1", 1, Duration.ofSeconds(10)).allowed())
                .isTrue();
        SlidingWindowRateLimiter.Decision rejected =
                limiter.acquire("auth-ip:127.0.0.1", 1, Duration.ofSeconds(10));
        assertThat(rejected.allowed()).isFalse();
        assertThat(rejected.retryAfterSeconds()).isEqualTo(2);
    }

    @Test
    void redisFailureFallsBackToProcessLocalSlidingWindow() {
        when(redis.execute(
                any(DefaultRedisScript.class),
                anyList(),
                any(Object[].class)
        )).thenThrow(new IllegalStateException("redis unavailable"));
        Clock clock = Clock.fixed(Instant.parse("2026-08-31T00:00:00Z"), ZoneOffset.UTC);
        SlidingWindowRateLimiter limiter = new SlidingWindowRateLimiter(redis, clock);

        assertThat(limiter.acquire("write-user:user-1", 1, Duration.ofMinutes(1)).allowed())
                .isTrue();
        SlidingWindowRateLimiter.Decision rejected =
                limiter.acquire("write-user:user-1", 1, Duration.ofMinutes(1));
        assertThat(rejected.allowed()).isFalse();
        assertThat(rejected.retryAfterSeconds()).isEqualTo(60);
    }

    @Test
    void slidingWindowPreventsDoubleBurstAcrossFixedWindowBoundary() {
        when(redis.execute(
                any(DefaultRedisScript.class),
                anyList(),
                any(Object[].class)
        )).thenThrow(new IllegalStateException("redis unavailable"));
        MutableClock clock = new MutableClock(Instant.parse("2026-08-31T00:00:59.900Z"));
        SlidingWindowRateLimiter limiter = new SlidingWindowRateLimiter(redis, clock);

        assertThat(limiter.acquire("boundary", 2, Duration.ofMinutes(1)).allowed()).isTrue();
        assertThat(limiter.acquire("boundary", 2, Duration.ofMinutes(1)).allowed()).isTrue();
        clock.set(Instant.parse("2026-08-31T00:01:00.100Z"));

        assertThat(limiter.acquire("boundary", 2, Duration.ofMinutes(1)).allowed()).isFalse();
    }

    private static final class MutableClock extends Clock {
        private final AtomicReference<Instant> instant;

        private MutableClock(Instant instant) {
            this.instant = new AtomicReference<>(instant);
        }

        private void set(Instant value) {
            instant.set(value);
        }

        @Override
        public ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return instant.get();
        }
    }
}
