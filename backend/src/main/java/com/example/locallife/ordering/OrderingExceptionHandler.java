package com.example.locallife.ordering;

import com.example.locallife.common.ApiResponse;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.github.resilience4j.bulkhead.BulkheadFullException;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
class OrderingExceptionHandler {
    private final Counter bulkheadRejections;
    private final Counter downstreamFailures;
    private final Counter circuitOpenRejections;

    OrderingExceptionHandler(MeterRegistry meterRegistry) {
        this.bulkheadRejections = Counter.builder("local.life.catalog.bulkhead.rejections")
                .description("Catalog read calls rejected by the semaphore bulkhead")
                .register(meterRegistry);
        this.downstreamFailures = Counter.builder("local.life.catalog.downstream.failures")
                .description("Catalog read calls mapped from downstream unavailability")
                .register(meterRegistry);
        this.circuitOpenRejections = Counter.builder("local.life.catalog.circuit.open.rejections")
                .description("Catalog read calls rejected by an open circuit")
                .register(meterRegistry);
    }

    @ExceptionHandler(CatalogUnavailableException.class)
    ResponseEntity<ApiResponse<Void>> handleCatalogUnavailable(
            CatalogUnavailableException exception
    ) {
        downstreamFailures.increment();
        return ResponseEntity
                .status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(ApiResponse.fail(exception.getMessage()));
    }

    @ExceptionHandler(CallNotPermittedException.class)
    ResponseEntity<ApiResponse<Void>> handleCatalogCircuitOpen(
            CallNotPermittedException exception
    ) {
        circuitOpenRejections.increment();
        return ResponseEntity
                .status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(ApiResponse.fail("商品服务暂时不可用"));
    }

    @ExceptionHandler(BulkheadFullException.class)
    ResponseEntity<ApiResponse<Void>> handleCatalogBulkheadFull(
            BulkheadFullException exception
    ) {
        bulkheadRejections.increment();
        return ResponseEntity
                .status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(ApiResponse.fail("商品服务暂时不可用"));
    }
}
