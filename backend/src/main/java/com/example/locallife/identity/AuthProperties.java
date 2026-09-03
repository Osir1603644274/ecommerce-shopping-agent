package com.example.locallife.identity;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties(prefix = "local-life.auth")
record AuthProperties(
        String issuer,
        String secret,
        Duration accessTtl,
        Duration refreshTtl
) {
    AuthProperties {
        if (secret == null || secret.getBytes(java.nio.charset.StandardCharsets.UTF_8).length < 32) {
            throw new IllegalArgumentException("local-life.auth.secret must contain at least 32 UTF-8 bytes");
        }
    }
}
