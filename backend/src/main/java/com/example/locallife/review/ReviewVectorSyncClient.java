package com.example.locallife.review;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.net.http.HttpClient;

@Component
public class ReviewVectorSyncClient {

    private static final Logger log = LoggerFactory.getLogger(ReviewVectorSyncClient.class);

    private final RestClient restClient;
    private final int maxAttempts;

    @Autowired
    public ReviewVectorSyncClient(
            RestClient.Builder restClientBuilder,
            @Value("${agent.base-url:http://localhost:8000}") String agentBaseUrl,
            @Value("${agent.review-sync.max-attempts:3}") int maxAttempts
    ) {
        HttpClient httpClient = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .build();
        this.restClient = restClientBuilder
                .requestFactory(new JdkClientHttpRequestFactory(httpClient))
                .baseUrl(agentBaseUrl)
                .build();
        this.maxAttempts = maxAttempts;
    }

    ReviewVectorSyncClient(RestClient restClient, int maxAttempts) {
        this.restClient = restClient;
        this.maxAttempts = maxAttempts;
    }

    public void upsert(ReviewResponse review, String shopName) {
        ReviewVectorSyncRequest request = new ReviewVectorSyncRequest(
                review.shopId(),
                shopName,
                review.content(),
                review.source(),
                review.language(),
                review.contentZh(),
                review.translationStatus(),
                review.tags()
        );
        executeWithRetry(
                "upsert",
                review.reviewId(),
                () -> restClient.put()
                        .uri("/internal/review-vectors/{reviewId}", review.reviewId())
                        .contentType(MediaType.APPLICATION_JSON)
                        .body(request)
                        .retrieve()
                        .toBodilessEntity()
        );
    }

    public void delete(String reviewId) {
        executeWithRetry(
                "delete",
                reviewId,
                () -> restClient.delete()
                        .uri("/internal/review-vectors/{reviewId}", reviewId)
                        .retrieve()
                        .toBodilessEntity()
        );
    }

    private void executeWithRetry(String operation, String reviewId, Runnable request) {
        RestClientException lastException = null;
        for (int attempt = 1; attempt <= maxAttempts; attempt++) {
            try {
                request.run();
                return;
            } catch (RestClientException exception) {
                lastException = exception;
                log.warn(
                        "评论向量同步失败: operation={}, reviewId={}, attempt={}/{}",
                        operation,
                        reviewId,
                        attempt,
                        maxAttempts
                );
            }
        }
        throw new ReviewVectorSyncException("评论向量同步失败", lastException);
    }
}
