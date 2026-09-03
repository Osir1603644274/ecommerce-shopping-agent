package com.example.locallife.ordering;

import com.example.locallife.common.ApiResponse;
import com.example.locallife.review.ReviewVectorSyncClient;
import feign.Retryer;
import io.github.resilience4j.bulkhead.Bulkhead;
import io.github.resilience4j.bulkhead.BulkheadFullException;
import io.github.resilience4j.bulkhead.BulkheadRegistry;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.aop.support.AopUtils;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@SpringBootTest(properties = {
        "local-life.deployment.role=trade",
        "local-life.deployment.require-internal-token=true",
        "local-life.deployment.internal-token=0123456789abcdef0123456789abcdef",
        "resilience4j.circuitbreaker.instances.catalogRead.sliding-window-type=COUNT_BASED",
        "resilience4j.circuitbreaker.instances.catalogRead.sliding-window-size=10",
        "resilience4j.circuitbreaker.instances.catalogRead.minimum-number-of-calls=5",
        "resilience4j.circuitbreaker.instances.catalogRead.failure-rate-threshold=50",
        "resilience4j.circuitbreaker.instances.catalogRead.wait-duration-in-open-state=10s",
        "resilience4j.bulkhead.instances.catalogRead.max-concurrent-calls=2",
        "resilience4j.bulkhead.instances.catalogRead.max-wait-duration=0ms"
})
@ActiveProfiles("cloud-trade")
class RemoteCommerceCatalogPortTests {
    @Autowired
    private CommerceCatalogPort catalog;

    @Autowired
    private Retryer retryer;

    @Autowired
    private CircuitBreakerRegistry circuitBreakerRegistry;

    @Autowired
    private BulkheadRegistry bulkheadRegistry;

    @MockitoBean
    private RemoteCommerceCatalogClient client;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @BeforeEach
    void resetCircuitBreaker() {
        circuitBreakerRegistry.circuitBreaker("catalogRead").reset();
    }

    @Test
    void currentReadOnlyCatalogFeignClientUsesNeverRetry() {
        CommerceItemSnapshot expected = new CommerceItemSnapshot(
                "PRODUCT", 1001L, "phone", 249900L, "CNY", 7L, "{}"
        );
        when(client.getItem("PRODUCT", 1001L,
                "0123456789abcdef0123456789abcdef"))
                .thenReturn(ApiResponse.ok(expected));

        assertThat(catalog.requireItem("PRODUCT", 1001L)).isEqualTo(expected);
        assertThat(retryer).isSameAs(Retryer.NEVER_RETRY);
        verify(client).getItem("PRODUCT", 1001L,
                "0123456789abcdef0123456789abcdef");
    }

    @Test
    void catalogReadCircuitOpensAfterTheFrozenFailureThreshold() {
        CircuitBreaker circuitBreaker =
                circuitBreakerRegistry.circuitBreaker("catalogRead");
        assertThat(AopUtils.isAopProxy(catalog)).isTrue();
        assertThat(circuitBreaker.getCircuitBreakerConfig().getMinimumNumberOfCalls())
                .isEqualTo(5);
        when(client.getItem("PRODUCT", 1001L,
                "0123456789abcdef0123456789abcdef"))
                .thenThrow(new IllegalStateException("catalog outage"));

        for (int attempt = 0; attempt < 5; attempt++) {
            assertThatThrownBy(() -> catalog.requireItem("PRODUCT", 1001L))
                    .isInstanceOf(IllegalStateException.class)
                    .hasMessage("catalog outage");
        }

        assertThat(circuitBreaker.getState()).isEqualTo(CircuitBreaker.State.OPEN);
        assertThatThrownBy(() -> catalog.requireItem("PRODUCT", 1001L))
                .isInstanceOf(CallNotPermittedException.class);
        verify(client, times(5)).getItem("PRODUCT", 1001L,
                "0123456789abcdef0123456789abcdef");
    }

    @Test
    void catalogReadSemaphoreBulkheadRejectsBeyondTwoConcurrentCallsWithoutWaiting() throws Exception {
        Bulkhead bulkhead = bulkheadRegistry.bulkhead("catalogRead");
        assertThat(bulkhead.getBulkheadConfig().getMaxConcurrentCalls()).isEqualTo(2);
        assertThat(bulkhead.getBulkheadConfig().getMaxWaitDuration()).isEqualTo(Duration.ZERO);

        CommerceItemSnapshot expected = new CommerceItemSnapshot(
                "PRODUCT", 1001L, "phone", 249900L, "CNY", 7L, "{}"
        );
        CountDownLatch enteredClient = new CountDownLatch(2);
        CountDownLatch releaseClient = new CountDownLatch(1);
        when(client.getItem("PRODUCT", 1001L,
                "0123456789abcdef0123456789abcdef"))
                .thenAnswer(invocation -> {
                    enteredClient.countDown();
                    if (!releaseClient.await(5, TimeUnit.SECONDS)) {
                        throw new IllegalStateException("test release timeout");
                    }
                    return ApiResponse.ok(expected);
                });

        CompletableFuture<CommerceItemSnapshot> first = CompletableFuture.supplyAsync(
                () -> catalog.requireItem("PRODUCT", 1001L));
        CompletableFuture<CommerceItemSnapshot> second = CompletableFuture.supplyAsync(
                () -> catalog.requireItem("PRODUCT", 1001L));
        assertThat(enteredClient.await(5, TimeUnit.SECONDS)).isTrue();

        try {
            assertThatThrownBy(() -> catalog.requireItem("PRODUCT", 1001L))
                    .isInstanceOf(BulkheadFullException.class);
            verify(client, times(2)).getItem("PRODUCT", 1001L,
                    "0123456789abcdef0123456789abcdef");
        } finally {
            releaseClient.countDown();
        }

        assertThat(first.get(5, TimeUnit.SECONDS)).isEqualTo(expected);
        assertThat(second.get(5, TimeUnit.SECONDS)).isEqualTo(expected);
    }
}
