package com.example.locallife.gateway;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties(prefix = "gateway.rate-limit")
record GatewayRateLimitProperties(
        boolean enabled,
        int authLimit,
        int writeLimit,
        Duration window
) {
    GatewayRateLimitProperties {
        if (authLimit < 1 || writeLimit < 1) {
            throw new IllegalArgumentException("gateway rate limits must be positive");
        }
        if (window == null || window.isZero() || window.isNegative()) {
            throw new IllegalArgumentException("gateway rate-limit window must be positive");
        }
    }
}
