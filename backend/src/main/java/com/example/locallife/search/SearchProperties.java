package com.example.locallife.search;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties("local-life.search")
public record SearchProperties(
        boolean enabled,
        String baseUrl,
        String indexPrefix,
        Duration connectTimeout,
        Duration requestTimeout,
        int failureThreshold,
        Duration openDuration,
        Duration reconcileDelay,
        int reconcileLimit
) {
    public SearchProperties {
        baseUrl = normalizeBaseUrl(baseUrl);
        indexPrefix = indexPrefix == null || indexPrefix.isBlank() ? "local-life" : indexPrefix;
        connectTimeout = connectTimeout == null ? Duration.ofSeconds(1) : connectTimeout;
        requestTimeout = requestTimeout == null ? Duration.ofSeconds(2) : requestTimeout;
        failureThreshold = failureThreshold <= 0 ? 3 : failureThreshold;
        openDuration = openDuration == null ? Duration.ofSeconds(30) : openDuration;
        reconcileDelay = reconcileDelay == null ? Duration.ofMinutes(5) : reconcileDelay;
        reconcileLimit = reconcileLimit <= 0 ? 1500 : Math.min(reconcileLimit, 10_000);
    }

    String productIndex() {
        // V2 starts a clean external-version namespace. Reusing the legacy
        // index would compare MySQL entityVersion=1..N with old ES internal
        // document versions and reject valid CDC updates as 409 conflicts.
        return indexPrefix + "-products-v2";
    }

    String shopIndex() {
        return indexPrefix + "-shops-v1";
    }

    private static String normalizeBaseUrl(String value) {
        String normalized = value == null || value.isBlank()
                ? "http://localhost:9200"
                : value.strip();
        while (normalized.endsWith("/")) {
            normalized = normalized.substring(0, normalized.length() - 1);
        }
        return normalized;
    }
}
