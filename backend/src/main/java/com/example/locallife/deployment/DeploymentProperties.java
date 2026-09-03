package com.example.locallife.deployment;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Set;

@ConfigurationProperties(prefix = "local-life.deployment")
public record DeploymentProperties(
        String role,
        boolean requireInternalToken,
        String internalToken
) {
    private static final Set<String> ROLES = Set.of("monolith", "catalog", "trade");

    public DeploymentProperties {
        role = role == null || role.isBlank()
                ? "monolith" : role.strip().toLowerCase(Locale.ROOT);
        internalToken = internalToken == null ? "" : internalToken;
        if (!ROLES.contains(role)) {
            throw new IllegalArgumentException(
                    "local-life.deployment.role must be monolith, catalog, or trade");
        }
        if (requireInternalToken
                && internalToken.getBytes(StandardCharsets.UTF_8).length < 32) {
            throw new IllegalArgumentException(
                    "local-life.deployment.internal-token must contain at least 32 UTF-8 bytes");
        }
    }
}
