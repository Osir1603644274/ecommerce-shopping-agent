package com.example.locallife.review;

import com.example.locallife.integration.DomainEventTypes;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.verifyNoInteractions;

@SpringBootTest(properties = "agent.review-sync.mode=outbox")
@Transactional
class ReviewOutboxIntegrationTests {
    @Autowired
    private ReviewService reviewService;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @Test
    void reviewAndVectorSyncIntentAreStoredInSameTransaction() {
        ReviewResponse review = reviewService.createReview(
                new CreateReviewRequest(
                        3L,
                        "异步 Outbox 评价测试",
                        List.of("可靠消息", "事务")
                ),
                "outbox-test-user"
        );

        String eventType = jdbcTemplate.queryForObject(
                "SELECT event_type FROM outbox_event WHERE aggregate_id = ?",
                String.class,
                review.reviewId()
        );
        String payload = jdbcTemplate.queryForObject(
                "SELECT payload_json FROM outbox_event WHERE aggregate_id = ?",
                String.class,
                review.reviewId()
        );
        assertThat(eventType).isEqualTo(DomainEventTypes.REVIEW_VECTOR_UPSERT_V1);
        assertThat(payload)
                .contains(review.reviewId())
                .contains("异步 Outbox 评价测试");
        verifyNoInteractions(reviewVectorSyncClient);
    }
}
