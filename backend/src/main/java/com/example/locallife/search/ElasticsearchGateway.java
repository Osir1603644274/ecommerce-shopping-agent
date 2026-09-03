package com.example.locallife.search;

import com.example.locallife.product.Product;
import com.example.locallife.shop.Shop;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

@Component
@ConditionalOnProperty(
        prefix = "local-life.search",
        name = "enabled",
        havingValue = "true"
)
class ElasticsearchGateway implements HealthIndicator {
    private static final Logger log = LoggerFactory.getLogger(ElasticsearchGateway.class);
    private final SearchProperties properties;
    private final ObjectMapper objectMapper;
    private final MeterRegistry meters;
    private final HttpClient httpClient;
    private final Clock clock;
    private final AtomicInteger consecutiveFailures = new AtomicInteger();
    private final AtomicReference<Instant> openUntil = new AtomicReference<>(Instant.EPOCH);

    @Autowired
    ElasticsearchGateway(
            SearchProperties properties,
            ObjectMapper objectMapper,
            MeterRegistry meters
    ) {
        this(properties, objectMapper, meters, Clock.systemUTC());
    }

    ElasticsearchGateway(
            SearchProperties properties,
            ObjectMapper objectMapper,
            MeterRegistry meters,
            Clock clock
    ) {
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.meters = meters;
        this.clock = clock;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(properties.connectTimeout())
                .build();
    }

    Optional<List<Long>> searchProducts(
            String query,
            String category,
            String brand,
            Long minPriceMinor,
            Long maxPriceMinor,
            int limit
    ) {
        List<Map<String, Object>> filters = new ArrayList<>();
        if (category != null) {
            filters.add(Map.of("bool", Map.of(
                    "should", List.of(
                            Map.of("term", Map.of("categoryL1", category)),
                            Map.of("term", Map.of("categoryL2", category)),
                            Map.of("term", Map.of("categoryL3", category)),
                            Map.of("wildcard", Map.of("categoryL1", Map.of("value", "*" + category + "*"))),
                            Map.of("wildcard", Map.of("categoryL2", Map.of("value", "*" + category + "*"))),
                            Map.of("wildcard", Map.of("categoryL3", Map.of("value", "*" + category + "*")))
                    ),
                    "minimum_should_match", 1
            )));
        }
        // Brand/price are TaskState hard conditions.  Do not trust a possibly
        // lagging ES copy to exclude candidates; ProductService re-reads MySQL
        // and applies them after lexical Top-50 recall.
        Map<String, Object> bool = new LinkedHashMap<>();
        bool.put("must", List.of(Map.of("multi_match", Map.of(
                "query", query,
                "fields", List.of("title^2", "attributeText"),
                "analyzer", "standard"
        ))));
        bool.put("filter", filters);
        return searchIds(
                "product",
                properties.productIndex(),
                Map.of("size", Math.min(Math.max(limit, 1), 50), "query", Map.of("bool", bool))
        );
    }

    Optional<List<Long>> searchShops(Long typeId, String name, int limit) {
        List<Map<String, Object>> filters = typeId == null
                ? List.of()
                : List.of(Map.of("term", Map.of("typeId", typeId)));
        Map<String, Object> bool = Map.of(
                "must", List.of(Map.of("multi_match", Map.of(
                        "query", name,
                        "fields", List.of("name^4", "address", "district"),
                        "fuzziness", "AUTO"
                ))),
                "filter", filters
        );
        return searchIds(
                "shop",
                properties.shopIndex(),
                Map.of("size", limit, "query", Map.of("bool", bool))
        );
    }

    void ensureIndices() {
        execute("index", () -> {
            ensureIndex(properties.productIndex(), productMapping());
            ensureIndex(properties.shopIndex(), shopMapping());
            return null;
        });
    }

    void indexProducts(List<Product> products) {
        bulk("product", properties.productIndex(), products.stream()
                .map(this::productDocument)
                .toList());
    }

    void indexProduct(Product product, long entityVersion) {
        execute("product-index", () -> {
            request(
                    "PUT",
                    "/" + properties.productIndex() + "/_doc/" + product.id()
                            + "?version=" + entityVersion + "&version_type=external_gte",
                    "application/json",
                    objectMapper.writeValueAsString(productDocument(product).document())
            );
            return null;
        });
    }

    void deleteProduct(long productId, long entityVersion) {
        execute("product-delete", () -> {
            request(
                    "DELETE",
                    "/" + properties.productIndex() + "/_doc/" + productId
                            + "?version=" + entityVersion + "&version_type=external_gte",
                    null,
                    null
            );
            return null;
        });
    }

    void indexShops(List<Shop> shops) {
        bulk("shop", properties.shopIndex(), shops.stream()
                .map(this::shopDocument)
                .toList());
    }

    void indexShop(Shop shop) {
        execute("shop-index", () -> {
            request(
                    "PUT",
                    "/" + properties.shopIndex() + "/_doc/" + shop.id(),
                    "application/json",
                    objectMapper.writeValueAsString(shopDocument(shop).document())
            );
            return null;
        });
    }

    boolean circuitOpen() {
        return Instant.now(clock).isBefore(openUntil.get());
    }

    @Override
    public Health health() {
        if (circuitOpen()) {
            return Health.status("DEGRADED")
                    .withDetail("reason", "circuit-open")
                    .withDetail("openUntil", openUntil.get().toString())
                    .build();
        }
        try {
            request("GET", "/_cluster/health", null, null);
            success("health");
            return Health.up().build();
        } catch (RuntimeException exception) {
            failure("health", exception);
            return Health.status("DEGRADED")
                    .withDetail("reason", "search-unavailable-database-fallback-active")
                    .build();
        }
    }

    private Optional<List<Long>> searchIds(
            String entity,
            String index,
            Map<String, Object> body
    ) {
        if (circuitOpen()) {
            meters.counter("local_life.search.fallback", "entity", entity, "reason", "circuit-open")
                    .increment();
            return Optional.empty();
        }
        try {
            JsonNode response = request(
                    "POST",
                    "/" + index + "/_search",
                    "application/json",
                    objectMapper.writeValueAsString(body)
            );
            List<Long> ids = new ArrayList<>();
            response.path("hits").path("hits").forEach(hit ->
                    ids.add(Long.valueOf(hit.path("_id").asText())));
            success(entity);
            return Optional.of(ids);
        } catch (RuntimeException exception) {
            failure(entity, exception);
            meters.counter(
                    "local_life.search.fallback",
                    "entity", entity,
                    "reason", "request-failure"
            ).increment();
            return Optional.empty();
        } catch (Exception exception) {
            failure(entity, exception);
            meters.counter(
                    "local_life.search.fallback",
                    "entity", entity,
                    "reason", "request-failure"
            ).increment();
            return Optional.empty();
        }
    }

    private void bulk(String entity, String index, List<SearchDocument> documents) {
        if (documents.isEmpty()) {
            return;
        }
        execute(entity + "-bulk", () -> {
            StringBuilder payload = new StringBuilder();
            for (SearchDocument document : documents) {
                Map<String, Object> metadata = new LinkedHashMap<>();
                metadata.put("_index", index);
                metadata.put("_id", document.id());
                if (document.entityVersion() != null) {
                    metadata.put("version", document.entityVersion());
                    metadata.put("version_type", "external_gte");
                }
                payload.append(objectMapper.writeValueAsString(
                        Map.of("index", metadata)
                )).append('\n');
                payload.append(objectMapper.writeValueAsString(document.document())).append('\n');
            }
            JsonNode result = request(
                    "POST",
                    "/_bulk",
                    "application/x-ndjson",
                    payload.toString()
            );
            if (result.path("errors").asBoolean(false)) {
                throw new IllegalStateException("Elasticsearch bulk response contains item errors");
            }
            return null;
        });
    }

    private void ensureIndex(String index, Map<String, Object> mapping) throws Exception {
        HttpResponse<String> head = send("HEAD", "/" + index, null, null);
        if (head.statusCode() == 200) {
            return;
        }
        if (head.statusCode() != 404) {
            throw statusFailure(head);
        }
        request(
                "PUT",
                "/" + index,
                "application/json",
                objectMapper.writeValueAsString(mapping)
        );
    }

    private JsonNode request(
            String method,
            String path,
            String contentType,
            String body
    ) {
        try {
            HttpResponse<String> response = send(method, path, contentType, body);
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw statusFailure(response);
            }
            return response.body() == null || response.body().isBlank()
                    ? objectMapper.createObjectNode()
                    : objectMapper.readTree(response.body());
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Elasticsearch request interrupted", exception);
        } catch (RuntimeException exception) {
            throw exception;
        } catch (Exception exception) {
            throw new IllegalStateException("Elasticsearch request failed", exception);
        }
    }

    private HttpResponse<String> send(
            String method,
            String path,
            String contentType,
            String body
    ) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder()
                .uri(URI.create(properties.baseUrl() + path))
                .timeout(properties.requestTimeout());
        if (contentType != null) {
            request.header("Content-Type", contentType);
        }
        request.method(
                method,
                body == null
                        ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8)
        );
        return httpClient.send(request.build(), HttpResponse.BodyHandlers.ofString());
    }

    private <T> T execute(String operation, CheckedSupplier<T> supplier) {
        if (circuitOpen()) {
            throw new IllegalStateException("Elasticsearch circuit is open");
        }
        try {
            T result = supplier.get();
            success(operation);
            return result;
        } catch (RuntimeException exception) {
            failure(operation, exception);
            throw exception;
        } catch (Exception exception) {
            failure(operation, exception);
            throw new IllegalStateException("Elasticsearch operation failed", exception);
        }
    }

    private void success(String operation) {
        consecutiveFailures.set(0);
        openUntil.set(Instant.EPOCH);
        meters.counter("local_life.search.requests", "operation", operation, "outcome", "success")
                .increment();
    }

    private void failure(String operation, Throwable failure) {
        int failures = consecutiveFailures.incrementAndGet();
        meters.counter("local_life.search.requests", "operation", operation, "outcome", "failure")
                .increment();
        if (failures >= properties.failureThreshold()) {
            openUntil.set(Instant.now(clock).plus(properties.openDuration()));
        }
        log.warn("Elasticsearch operation failed, operation={}, consecutiveFailures={}",
                operation, failures, failure);
    }

    private IllegalStateException statusFailure(HttpResponse<String> response) {
        String body = response.body() == null ? "" : response.body();
        if (body.length() > 500) {
            body = body.substring(0, 500);
        }
        return new IllegalStateException(
                "Elasticsearch returned HTTP " + response.statusCode() + ": " + body
        );
    }

    private SearchDocument productDocument(Product product) {
        Map<String, Object> document = new LinkedHashMap<>();
        document.put("title", product.title());
        document.put("brand", product.brand() == null
                ? null
                : product.brand().toLowerCase(Locale.ROOT));
        document.put("categoryL1", product.categoryL1());
        document.put("categoryL2", product.categoryL2());
        document.put("categoryL3", product.categoryL3());
        document.put("snapshotPriceMinor", product.snapshotPriceMinor());
        document.put("priceStatus", product.priceStatus());
        document.put("lifecycleStatus", product.lifecycleStatus());
        document.put("entityVersion", product.entityVersion());
        document.put("attributeText", product.attributeText());
        return new SearchDocument(product.id(), document, product.entityVersion());
    }

    private SearchDocument shopDocument(Shop shop) {
        Map<String, Object> document = new LinkedHashMap<>();
        document.put("name", shop.name());
        document.put("typeId", shop.typeId());
        document.put("address", shop.address());
        document.put("district", shop.district());
        document.put("avgPrice", shop.avgPrice());
        return new SearchDocument(shop.id(), document, null);
    }

    private static Map<String, Object> productMapping() {
        return Map.of(
                "settings", Map.of("analysis", Map.of("normalizer", Map.of(
                        "lowercase_normalizer",
                        Map.of("type", "custom", "filter", List.of("lowercase"))
                ))),
                "mappings", Map.of("properties", Map.of(
                        "title", Map.of("type", "text", "analyzer", "standard"),
                        "brand", Map.of(
                                "type", "keyword",
                                "ignore_above", 256,
                                "normalizer", "lowercase_normalizer"
                        ),
                        "categoryL1", Map.of("type", "keyword"),
                        "categoryL2", Map.of("type", "keyword"),
                        "categoryL3", Map.of("type", "keyword"),
                        "snapshotPriceMinor", Map.of("type", "long"),
                        "priceStatus", Map.of("type", "keyword"),
                        "lifecycleStatus", Map.of("type", "keyword"),
                        "entityVersion", Map.of("type", "long"),
                        "attributeText", Map.of("type", "text", "analyzer", "standard")
                ))
        );
    }

    private static Map<String, Object> shopMapping() {
        return Map.of("mappings", Map.of("properties", Map.of(
                "name", Map.of("type", "text"),
                "typeId", Map.of("type", "long"),
                "address", Map.of("type", "text"),
                "district", Map.of("type", "text"),
                "avgPrice", Map.of("type", "integer")
        )));
    }

    private record SearchDocument(Long id, Map<String, Object> document, Long entityVersion) {
    }

    @FunctionalInterface
    private interface CheckedSupplier<T> {
        T get() throws Exception;
    }
}
