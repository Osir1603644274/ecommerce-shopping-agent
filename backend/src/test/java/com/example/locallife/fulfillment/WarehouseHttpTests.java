package com.example.locallife.fulfillment;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;
import java.net.InetSocketAddress;
import java.net.URI;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.concurrent.atomic.AtomicReference;
import static org.assertj.core.api.Assertions.*;

class WarehouseHttpTests {
    @Test void committedRemoteResultIsRecoveredByLookupAfterAnErrorResponse() throws Exception {
        ObjectMapper json = new ObjectMapper();
        AtomicReference<WarehouseReceipt> committed = new AtomicReference<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/shipments", exchange -> {
            if (!"Bearer fixture-token".equals(exchange.getRequestHeaders().getFirst("Authorization"))) {
                exchange.sendResponseHeaders(401, -1); exchange.close(); return;
            }
            if ("POST".equals(exchange.getRequestMethod())) {
                String command = new String(exchange.getRequestBody().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
                var request = json.readValue(command, WarehouseCommand.class);
                committed.set(new WarehouseReceipt(request.requestKey(), request.orderId(), FulfillmentEvents.hash(command), "SIM-HTTP"));
                exchange.sendResponseHeaders(503, -1); // Side effect committed; caller cannot infer failure.
            } else if (committed.get() == null) {
                exchange.sendResponseHeaders(404, -1);
            } else {
                byte[] response = json.writeValueAsBytes(committed.get());
                exchange.sendResponseHeaders(200, response.length); exchange.getResponseBody().write(response);
            }
            exchange.close();
        });
        server.start();
        try {
            var properties = new FulfillmentProperties(true, false, false,
                    URI.create("http://127.0.0.1:" + server.getAddress().getPort()), "fixture-token",
                    Duration.ofSeconds(1), Duration.ofSeconds(30), 1, 1, 2);
            var gateway = new HttpWarehouseGateway(properties, json);
            String command = json.writeValueAsString(new WarehouseCommand("key-1", "order-1", "PRODUCT", 1001L, 1));
            var task = new FulfillmentTask("order-1", "key-1", command, "DISPATCHING", 1, 1, "worker",
                    null, null, null, null, null, LocalDateTime.now(), LocalDateTime.now());
            assertThat(gateway.lookup(task.requestKey())).isEmpty();
            assertThatThrownBy(() -> gateway.dispatch(task)).hasMessageContaining("503");
            assertThat(gateway.lookup(task.requestKey())).contains(committed.get());
            assertThat(committed.get().commandHash()).isEqualTo(FulfillmentEvents.hash(command));
        } finally { server.stop(0); }
    }
}
