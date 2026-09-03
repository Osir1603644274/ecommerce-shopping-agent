package com.example.locallife.review;

import org.junit.jupiter.api.Test;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.client.ExpectedCount;
import org.springframework.test.web.client.MockRestServiceServer;
import org.springframework.web.client.RestClient;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.method;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.content;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withStatus;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

class ReviewVectorSyncClientTests {

    @Test
    void upsertRetriesUntilThirdAttemptSucceeds() {
        RestClient.Builder builder = RestClient.builder();
        MockRestServiceServer server = MockRestServiceServer.bindTo(builder).build();
        ReviewVectorSyncClient client = new ReviewVectorSyncClient(
                builder.baseUrl("http://agent.test").build(),
                3
        );
        String uri = "http://agent.test/internal/review-vectors/review-business-123";
        server.expect(ExpectedCount.times(2), requestTo(uri))
                .andExpect(method(HttpMethod.PUT))
                .andExpect(content().contentType("application/json"))
                .andRespond(withStatus(HttpStatus.SERVICE_UNAVAILABLE));
        server.expect(requestTo(uri))
                .andExpect(method(HttpMethod.PUT))
                .andExpect(content().json("""
                        {
                          "shopId": 3,
                          "shopName": "清晨手冲咖啡",
                          "content": "适合办公",
                          "source": "user",
                          "language": "zh",
                          "contentZh": null,
                          "translationStatus": "not_required",
                          "tags": ["办公", "插座"]
                        }
                        """))
                .andRespond(withSuccess());

        client.upsert(
                new ReviewResponse(
                        "review-business-123",
                        3L,
                        "清晨手冲咖啡",
                        "适合办公",
                        "user",
                        "zh",
                        null,
                        "not_required",
                        List.of("办公", "插座")
                ),
                "清晨手冲咖啡"
        );

        server.verify();
    }

    @Test
    void deleteThrowsAfterThreeFailedAttempts() {
        RestClient.Builder builder = RestClient.builder();
        MockRestServiceServer server = MockRestServiceServer.bindTo(builder).build();
        ReviewVectorSyncClient client = new ReviewVectorSyncClient(
                builder.baseUrl("http://agent.test").build(),
                3
        );
        String uri = "http://agent.test/internal/review-vectors/review-business-123";
        server.expect(ExpectedCount.times(3), requestTo(uri))
                .andExpect(method(HttpMethod.DELETE))
                .andRespond(withStatus(HttpStatus.SERVICE_UNAVAILABLE));

        assertThrows(
                ReviewVectorSyncException.class,
                () -> client.delete("review-business-123")
        );
        server.verify();
    }
}
