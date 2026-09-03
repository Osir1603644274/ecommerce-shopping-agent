package com.example.locallife.ordering;

import io.github.resilience4j.bulkhead.BulkheadFullException;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;

class OrderingExceptionHandlerTests {
    @Test
    void openCatalogCircuitMapsToServiceUnavailable() {
        SimpleMeterRegistry meterRegistry = new SimpleMeterRegistry();
        OrderingExceptionHandler handler = new OrderingExceptionHandler(meterRegistry);

        var response = handler.handleCatalogCircuitOpen(
                mock(CallNotPermittedException.class)
        );

        assertThat(response.getStatusCode().value()).isEqualTo(503);
        assertThat(response.getBody()).isNotNull();
        assertThat(response.getBody().success()).isFalse();
        assertThat(response.getBody().message()).contains("商品服务暂时不可用");
        assertThat(meterRegistry.counter("local.life.catalog.circuit.open.rejections").count())
                .isEqualTo(1.0);
    }

    @Test
    void fullCatalogBulkheadMapsToTheSameServiceUnavailableContract() {
        SimpleMeterRegistry meterRegistry = new SimpleMeterRegistry();
        OrderingExceptionHandler handler = new OrderingExceptionHandler(meterRegistry);

        var response = handler.handleCatalogBulkheadFull(
                mock(BulkheadFullException.class)
        );

        assertThat(response.getStatusCode().value()).isEqualTo(503);
        assertThat(response.getBody()).isNotNull();
        assertThat(response.getBody().success()).isFalse();
        assertThat(response.getBody().message()).isEqualTo("商品服务暂时不可用");
        assertThat(meterRegistry.counter("local.life.catalog.bulkhead.rejections").count())
                .isEqualTo(1.0);
    }

    @Test
    void unavailableCatalogIncrementsOnlyTheDownstreamFailureCounter() {
        SimpleMeterRegistry meterRegistry = new SimpleMeterRegistry();
        OrderingExceptionHandler handler = new OrderingExceptionHandler(meterRegistry);

        var response = handler.handleCatalogUnavailable(
                new CatalogUnavailableException("商品服务调用失败", new IllegalStateException("outage"))
        );

        assertThat(response.getStatusCode().value()).isEqualTo(503);
        assertThat(meterRegistry.counter("local.life.catalog.downstream.failures").count())
                .isEqualTo(1.0);
        assertThat(meterRegistry.counter("local.life.catalog.bulkhead.rejections").count())
                .isZero();
        assertThat(meterRegistry.counter("local.life.catalog.circuit.open.rejections").count())
                .isZero();
    }
}
