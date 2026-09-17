package com.example.locallife.search;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class ElasticsearchGatewayTests {
    private HttpServer server;

    @AfterEach
    void stopServer() {
        if (server != null) {
            server.stop(0);
        }
    }

    @Test
    void returnsTop50LexicalIdsWithoutTrustingEsForCommerceFacts() throws Exception {
        AtomicReference<String> requestBody = new AtomicReference<>();
        server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/catalog-products-v2/_search", exchange -> {
            requestBody.set(readBody(exchange));
            respond(exchange, 200, """
                    {"hits":{"hits":[{"_id":"42"},{"_id":"7"}]}}
                    """);
        });
        server.start();
        ElasticsearchGateway gateway = gateway(
                "http://localhost:" + server.getAddress().getPort(), 3);

        Optional<List<Long>> result = gateway.searchProducts(
                "gaming laptop", "laptop", "Acme", 500_00L, 900_00L, 10);

        assertThat(result).contains(List.of(42L, 7L));
        assertThat(requestBody.get())
                .contains("gaming laptop")
                .contains("\"analyzer\":\"standard\"")
                .contains("*laptop*")
                .doesNotContain("snapshotPriceMinor")
                .doesNotContain("priceStatus")
                .doesNotContain("acme");
    }

    @Test
    void opensCircuitAndReturnsFallbackSignalWhenSearchIsUnavailable() throws Exception {
        server = HttpServer.create(new InetSocketAddress(0), 0);
        int port = server.getAddress().getPort();
        server.start();
        server.stop(0);
        server = null;
        ElasticsearchGateway gateway = gateway("http://localhost:" + port, 1);

        Optional<List<Long>> result = gateway.searchProducts(
                "phone", null, null, null, null, 10);

        assertThat(result).isEmpty();
        assertThat(gateway.circuitOpen()).isTrue();
        assertThat(gateway.health().getStatus().getCode()).isEqualTo("DEGRADED");
    }

    @Test
    void replayedDocumentDeletionSucceedsWithoutOpeningCircuit() throws Exception {
        server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/catalog-products-v2/_doc/42", exchange ->
                respond(exchange,404,"{\"_id\":\"42\",\"result\":\"not_found\"}"));
        server.start();
        var gateway=gateway("http://localhost:"+server.getAddress().getPort(),1);
        gateway.deleteProduct(42,3);
        gateway.deleteProduct(42,3);
        assertThat(gateway.circuitOpen()).isFalse();
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(ints={404,409})
    void missingIndexAndVersionConflictRemainFailures(int status) throws Exception {
        server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/catalog-products-v2/_doc/42", exchange ->
                respond(exchange,status,"{\"error\":{\"type\":\"unrecoverable_without_repair\"}}"));
        server.start();
        var gateway=gateway("http://localhost:"+server.getAddress().getPort(),1);
        assertThatThrownBy(()->gateway.deleteProduct(42,3)).isInstanceOf(IllegalStateException.class);
        assertThat(gateway.circuitOpen()).isTrue();
    }

    private static ElasticsearchGateway gateway(String baseUrl, int failureThreshold) {
        SearchProperties properties = new SearchProperties(
                true,
                baseUrl,
                "catalog",
                Duration.ofMillis(200),
                Duration.ofMillis(300),
                failureThreshold,
                Duration.ofSeconds(30),
                Duration.ofMinutes(5),
                1500
        );
        return new ElasticsearchGateway(
                properties,
                new ObjectMapper(),
                new SimpleMeterRegistry()
        );
    }

    private static String readBody(HttpExchange exchange) throws IOException {
        return new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
    }

    private static void respond(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }
}
