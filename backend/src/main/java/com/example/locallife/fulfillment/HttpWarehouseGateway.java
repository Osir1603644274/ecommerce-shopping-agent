package com.example.locallife.fulfillment;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Component;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Optional;

@Component
class HttpWarehouseGateway implements WarehouseGateway {
    private final FulfillmentProperties properties;
    private final ObjectMapper json;
    private final HttpClient client;

    HttpWarehouseGateway(FulfillmentProperties properties, ObjectMapper json) {
        this.properties = properties; this.json = json;
        this.client = HttpClient.newBuilder().connectTimeout(properties.requestTimeout()).build();
    }

    @Override public Optional<WarehouseReceipt> lookup(String requestKey) {
        var request = request("/shipments/by-request/" + URLEncoder.encode(requestKey, StandardCharsets.UTF_8)).GET().build();
        var response = send(request);
        if (response.statusCode() == 404) return Optional.empty();
        return Optional.of(receipt(response));
    }

    @Override public WarehouseReceipt dispatch(FulfillmentTask task) {
        return receipt(send(request("/shipments").header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(task.commandJson(), StandardCharsets.UTF_8)).build()));
    }

    private HttpRequest.Builder request(String path) {
        return HttpRequest.newBuilder(properties.warehouseUrl().resolve(path))
                .timeout(properties.requestTimeout()).header("Authorization", "Bearer " + properties.warehouseToken());
    }

    private HttpResponse<String> send(HttpRequest request) {
        try { return client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8)); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new IllegalStateException("Warehouse call interrupted", e); }
        catch (java.io.IOException e) { throw new IllegalStateException("Warehouse outcome unknown", e); }
    }

    private WarehouseReceipt receipt(HttpResponse<String> response) {
        if (response.statusCode() != 200 && response.statusCode() != 201)
            throw new IllegalStateException("Warehouse returned HTTP " + response.statusCode());
        try { return json.readValue(response.body(), WarehouseReceipt.class); }
        catch (Exception e) { throw new IllegalStateException("Invalid warehouse receipt", e); }
    }
}
