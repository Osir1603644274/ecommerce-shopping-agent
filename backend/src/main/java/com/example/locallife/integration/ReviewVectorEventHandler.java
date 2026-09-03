package com.example.locallife.integration;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.net.http.HttpClient;
import java.util.List;

@Component
class ReviewVectorEventHandler implements InboundEventHandler {
    private final ObjectMapper objectMapper;
    private final RestClient restClient;

    ReviewVectorEventHandler(
            ObjectMapper objectMapper,
            RestClient.Builder builder,
            @Value("${agent.base-url:http://localhost:8000}") String agentBaseUrl
    ) {
        this.objectMapper = objectMapper;
        this.restClient = builder
                .requestFactory(new JdkClientHttpRequestFactory(HttpClient.newHttpClient()))
                .baseUrl(agentBaseUrl)
                .build();
    }

    @Override
    public boolean supports(String eventType) {
        return DomainEventTypes.REVIEW_VECTOR_UPSERT_V1.equals(eventType)
                || DomainEventTypes.REVIEW_VECTOR_DELETE_V1.equals(eventType);
    }

    @Override
    public void handle(EventEnvelope event) {
        JsonNode payload = parse(event.payloadJson());
        String reviewId = required(payload, "reviewId");
        if (DomainEventTypes.REVIEW_VECTOR_DELETE_V1.equals(event.eventType())) {
            restClient.delete()
                    .uri("/internal/review-vectors/{reviewId}", reviewId)
                    .header("X-Event-Id", event.id())
                    .retrieve()
                    .toBodilessEntity();
            return;
        }
        restClient.put()
                .uri("/internal/review-vectors/{reviewId}", reviewId)
                .header("X-Event-Id", event.id())
                .contentType(MediaType.APPLICATION_JSON)
                .body(new VectorRequest(
                        payload.path("shopId").asLong(),
                        required(payload, "shopName"),
                        required(payload, "content"),
                        required(payload, "source"),
                        required(payload, "language"),
                        nullableText(payload, "contentZh"),
                        required(payload, "translationStatus"),
                        objectMapper.convertValue(payload.path("tags"), List.class)
                ))
                .retrieve()
                .toBodilessEntity();
    }

    private JsonNode parse(String value) {
        try {
            return objectMapper.readTree(value);
        } catch (Exception exception) {
            throw new IllegalArgumentException("评价事件负载不是合法 JSON", exception);
        }
    }

    private static String required(JsonNode node, String field) {
        String value = nullableText(node, field);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("评价事件缺少字段: " + field);
        }
        return value;
    }

    private static String nullableText(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return value == null || value.isNull() ? null : value.asText();
    }

    private record VectorRequest(
            Long shopId,
            String shopName,
            String content,
            String source,
            String language,
            String contentZh,
            String translationStatus,
            List<String> tags
    ) {
    }
}
