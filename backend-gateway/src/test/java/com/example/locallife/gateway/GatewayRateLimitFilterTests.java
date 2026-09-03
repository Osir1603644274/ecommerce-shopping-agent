package com.example.locallife.gateway;

import org.junit.jupiter.api.Test;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.mock.http.server.reactive.MockServerHttpRequest;
import org.springframework.mock.web.server.MockServerWebExchange;
import org.springframework.web.server.WebFilterChain;
import reactor.core.publisher.Mono;

import java.time.Duration;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class GatewayRateLimitFilterTests {
    @Test
    void rejectsLimitedWriteAndReturnsRetryAfter() {
        ReactiveSlidingWindowRateLimiter limiter = mock(ReactiveSlidingWindowRateLimiter.class);
        when(limiter.acquire(any(), anyInt(), eq(Duration.ofMinutes(1))))
                .thenReturn(Mono.just(new ReactiveSlidingWindowRateLimiter.Decision(false, 3)));
        GatewayRateLimitFilter filter = new GatewayRateLimitFilter(
                limiter, new GatewayRateLimitProperties(true, 20, 120, Duration.ofMinutes(1)));
        MockServerWebExchange exchange = MockServerWebExchange.from(
                MockServerHttpRequest.method(HttpMethod.POST, "/api/orders/123"));
        AtomicBoolean called = new AtomicBoolean();
        WebFilterChain chain = ignored -> {
            called.set(true);
            return Mono.empty();
        };

        filter.filter(exchange, chain).block(Duration.ofSeconds(2));

        assertThat(called).isFalse();
        assertThat(exchange.getResponse().getStatusCode()).isEqualTo(HttpStatus.TOO_MANY_REQUESTS);
        assertThat(exchange.getResponse().getHeaders().getFirst("Retry-After")).isEqualTo("3");
    }
}
