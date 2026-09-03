package com.example.locallife.identity;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties(prefix = "local-life.rate-limit")
record RateLimitProperties(
        boolean enabled,
        int authLimit,
        int writeLimit,
        Duration window
) {
}
