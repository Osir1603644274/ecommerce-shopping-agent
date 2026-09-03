package com.example.locallife.flashsale;

import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class FlashSaleConcurrencyMetricsTests {
    @Test
    void recordsHttpAndRedisScriptActivityAsSeparateScopes() {
        SimpleMeterRegistry registry = new SimpleMeterRegistry();
        FlashSaleConcurrencyMetrics metrics = new FlashSaleConcurrencyMetrics(registry);

        var firstHttp = metrics.enterHttpRequest();
        var secondHttp = metrics.enterHttpRequest();
        var redisCall = metrics.enterRedisScriptCall();

        assertThat(value(registry, FlashSaleConcurrencyMetrics.HTTP_ACTIVE)).isEqualTo(2.0);
        assertThat(value(registry, FlashSaleConcurrencyMetrics.HTTP_PEAK)).isEqualTo(2.0);
        assertThat(value(registry, FlashSaleConcurrencyMetrics.REDIS_SCRIPT_IN_FLIGHT))
                .isEqualTo(1.0);
        assertThat(value(registry, FlashSaleConcurrencyMetrics.REDIS_SCRIPT_PEAK))
                .isEqualTo(1.0);

        redisCall.close();
        firstHttp.close();
        firstHttp.close();
        secondHttp.close();

        assertThat(value(registry, FlashSaleConcurrencyMetrics.HTTP_ACTIVE)).isZero();
        assertThat(value(registry, FlashSaleConcurrencyMetrics.REDIS_SCRIPT_IN_FLIGHT)).isZero();
        assertThat(registry.counter(FlashSaleConcurrencyMetrics.HTTP_STARTED).count())
                .isEqualTo(2.0);
        assertThat(registry.counter(FlashSaleConcurrencyMetrics.HTTP_COMPLETED).count())
                .isEqualTo(2.0);
        assertThat(registry.counter(FlashSaleConcurrencyMetrics.REDIS_SCRIPT_STARTED).count())
                .isEqualTo(1.0);
        assertThat(registry.counter(FlashSaleConcurrencyMetrics.REDIS_SCRIPT_COMPLETED).count())
                .isEqualTo(1.0);
    }

    private static double value(SimpleMeterRegistry registry, String name) {
        return registry.get(name).gauge().value();
    }
}
