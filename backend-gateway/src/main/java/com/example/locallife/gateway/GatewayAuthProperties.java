package com.example.locallife.gateway;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.nio.charset.StandardCharsets;

@ConfigurationProperties(prefix = "gateway.auth")
record GatewayAuthProperties(String issuer, String secret) {
    GatewayAuthProperties {
        if (issuer == null || issuer.isBlank()) {
            throw new IllegalArgumentException("gateway.auth.issuer is required");
        }
        if (secret == null || secret.getBytes(StandardCharsets.UTF_8).length < 32) {
            throw new IllegalArgumentException(
                    "gateway.auth.secret must contain at least 32 UTF-8 bytes");
        }
    }
}
