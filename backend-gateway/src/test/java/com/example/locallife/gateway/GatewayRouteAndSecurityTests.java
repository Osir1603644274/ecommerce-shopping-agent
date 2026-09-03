package com.example.locallife.gateway;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.cloud.gateway.route.RouteDefinitionLocator;
import org.springframework.test.web.reactive.server.WebTestClient;

import java.time.Duration;
import java.util.Map;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
        "gateway.auth.secret=0123456789abcdef0123456789abcdef",
        "gateway.rate-limit.enabled=false",
        "spring.cloud.gateway.httpclient.connect-timeout=100",
        "spring.cloud.gateway.httpclient.response-timeout=PT0.2S"
})
class GatewayRouteAndSecurityTests {
    @LocalServerPort
    private int port;

    @Autowired
    private RouteDefinitionLocator routes;

    @Test
    void routesCurrentCatalogAndTradeSurfacesToSeparateServices() {
        Map<String, String> definitions = routes.getRouteDefinitions()
                .collectList().block(Duration.ofSeconds(5)).stream()
                .collect(Collectors.toMap(
                        definition -> definition.getId(),
                        definition -> definition.getUri().toString()));

        assertThat(definitions).containsEntry(
                "catalog-search-service", "http://catalog-search-service:8081");
        assertThat(definitions).containsEntry(
                "trade-service", "http://trade-service:8082");
    }

    @Test
    void rejectsHighRiskWriteWithoutJwtBeforeRouting() {
        WebTestClient.bindToServer()
                .baseUrl("http://127.0.0.1:" + port)
                .responseTimeout(Duration.ofSeconds(3))
                .build()
                .post().uri("/api/orders")
                .exchange()
                .expectStatus().isUnauthorized()
                .expectBody()
                .jsonPath("$.success").isEqualTo(false);
    }
}
