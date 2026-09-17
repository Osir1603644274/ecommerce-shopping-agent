package com.example.locallife.review;

import com.example.locallife.integration.DomainEventTypes;
import com.example.locallife.integration.EventEnvelope;
import com.example.locallife.integration.InboundEventHandler;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.net.http.HttpClient;
import java.time.Duration;
import java.util.List;

@Component
class ReviewVectorEventHandler implements InboundEventHandler {
    private final ObjectMapper objectMapper;
    private final RestClient restClient;
    private final ReviewProjectionSource source;

    ReviewVectorEventHandler(
            ObjectMapper objectMapper,
            RestClient.Builder builder,
            ReviewProjectionSource source,
            @Value("${agent.base-url:http://localhost:8000}") String agentBaseUrl
    ) {
        this.objectMapper = objectMapper;
        this.source = source;
        var factory = new JdkClientHttpRequestFactory(HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(2)).build());
        factory.setReadTimeout(Duration.ofSeconds(10));
        this.restClient = builder
                .requestFactory(factory)
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
        if (!"REVIEW".equals(event.aggregateType()) || !reviewId.equals(event.aggregateId()))
            throw new IllegalArgumentException("Review event identity mismatch");
        var snapshot = source.snapshot(reviewId);
        var review = snapshot.review();
        if (review == null) {
            restClient.delete()
                    .uri("/internal/review-vectors/{reviewId}", reviewId)
                    .header("X-Event-Id", event.id())
                    .header("X-Projection-Revision", Long.toString(snapshot.revision()))
                    .retrieve()
                    .toBodilessEntity();
            return;
        }
        restClient.put()
                .uri("/internal/review-vectors/{reviewId}", reviewId)
                .header("X-Event-Id", event.id())
                .header("X-Projection-Revision", Long.toString(snapshot.revision()))
                .contentType(MediaType.APPLICATION_JSON)
                .body(new VectorRequest(
                        review.shopId(), snapshot.shopName(), review.content(), review.source(),
                        review.language(), review.contentZh(), review.translationStatus(),
                        objectMapper.convertValue(parse(review.tags()), List.class)
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
