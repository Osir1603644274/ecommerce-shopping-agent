package com.example.locallife.flashsale;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.stereotype.Component;

import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

@Component
public class FlashSaleConcurrencyMetrics {
    static final String HTTP_ACTIVE = "local.life.flash.sale.purchase.http.active";
    static final String HTTP_PEAK = "local.life.flash.sale.purchase.http.peak";
    static final String HTTP_STARTED = "local.life.flash.sale.purchase.http.started";
    static final String HTTP_COMPLETED = "local.life.flash.sale.purchase.http.completed";
    static final String REDIS_SCRIPT_IN_FLIGHT =
            "local.life.flash.sale.purchase.redis.script.inflight";
    static final String REDIS_SCRIPT_PEAK =
            "local.life.flash.sale.purchase.redis.script.peak";
    static final String REDIS_SCRIPT_STARTED =
            "local.life.flash.sale.purchase.redis.script.started";
    static final String REDIS_SCRIPT_COMPLETED =
            "local.life.flash.sale.purchase.redis.script.completed";

    private final AtomicInteger httpActive = new AtomicInteger();
    private final AtomicInteger httpPeak = new AtomicInteger();
    private final AtomicInteger redisScriptInFlight = new AtomicInteger();
    private final AtomicInteger redisScriptPeak = new AtomicInteger();
    private final Counter httpStarted;
    private final Counter httpCompleted;
    private final Counter redisScriptStarted;
    private final Counter redisScriptCompleted;

    public FlashSaleConcurrencyMetrics(MeterRegistry meterRegistry) {
        Gauge.builder(HTTP_ACTIVE, httpActive, AtomicInteger::get)
                .description("Active flash-sale purchase HTTP handlers")
                .register(meterRegistry);
        Gauge.builder(HTTP_PEAK, httpPeak, AtomicInteger::get)
                .description("Peak active flash-sale purchase HTTP handlers since process start")
                .register(meterRegistry);
        Gauge.builder(REDIS_SCRIPT_IN_FLIGHT, redisScriptInFlight, AtomicInteger::get)
                .description("Flash-sale purchase Redis script calls in flight from this process")
                .register(meterRegistry);
        Gauge.builder(REDIS_SCRIPT_PEAK, redisScriptPeak, AtomicInteger::get)
                .description("Peak flash-sale purchase Redis script calls in flight since process start")
                .register(meterRegistry);
        httpStarted = Counter.builder(HTTP_STARTED)
                .description("Flash-sale purchase HTTP handlers entered")
                .register(meterRegistry);
        httpCompleted = Counter.builder(HTTP_COMPLETED)
                .description("Flash-sale purchase HTTP handlers completed")
                .register(meterRegistry);
        redisScriptStarted = Counter.builder(REDIS_SCRIPT_STARTED)
                .description("Flash-sale purchase Redis script calls started")
                .register(meterRegistry);
        redisScriptCompleted = Counter.builder(REDIS_SCRIPT_COMPLETED)
                .description("Flash-sale purchase Redis script calls completed")
                .register(meterRegistry);
    }

    public Scope enterHttpRequest() {
        httpStarted.increment();
        int current = httpActive.incrementAndGet();
        httpPeak.accumulateAndGet(current, Math::max);
        return new Scope(() -> {
            httpActive.decrementAndGet();
            httpCompleted.increment();
        });
    }

    public Scope enterRedisScriptCall() {
        redisScriptStarted.increment();
        int current = redisScriptInFlight.incrementAndGet();
        redisScriptPeak.accumulateAndGet(current, Math::max);
        return new Scope(() -> {
            redisScriptInFlight.decrementAndGet();
            redisScriptCompleted.increment();
        });
    }

    public static final class Scope implements AutoCloseable {
        private final AtomicBoolean closed = new AtomicBoolean();
        private final Runnable onClose;

        private Scope(Runnable onClose) {
            this.onClose = onClose;
        }

        @Override
        public void close() {
            if (closed.compareAndSet(false, true)) {
                onClose.run();
            }
        }
    }
}
