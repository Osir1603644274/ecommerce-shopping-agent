package com.example.locallife.integration;

import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import java.time.LocalDateTime;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(properties = "local-life.messaging.max-attempts=1")
class InboxDeadLetterIntegrationTests {
    @Autowired
    private InboxClaimService inboxClaims;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @AfterEach
    void cleanUp() {
        jdbcTemplate.update("DELETE FROM dead_letter_event");
        jdbcTemplate.update("DELETE FROM inbox_event");
    }

    @Test
    void permanentHandlerFailureMovesInboxEventToDeadLetter() {
        EventEnvelope event = new EventEnvelope(
                "event-dead",
                "REVIEW",
                "review-dead",
                DomainEventTypes.REVIEW_VECTOR_DELETE_V1,
                "{\"reviewId\":\"review-dead\"}",
                LocalDateTime.now()
        );

        assertThat(inboxClaims.claim("consumer-dead", event)).isTrue();
        assertThat(inboxClaims.failed(
                "consumer-dead", event, new IllegalStateException("downstream unavailable")
        )).isTrue();

        String status = jdbcTemplate.queryForObject(
                """
                SELECT status FROM inbox_event
                WHERE consumer_name = ? AND event_id = ?
                """,
                String.class,
                "consumer-dead",
                event.id()
        );
        Integer deadLetters = jdbcTemplate.queryForObject(
                """
                SELECT COUNT(*) FROM dead_letter_event
                WHERE source = 'INBOX:consumer-dead' AND event_id = ?
                """,
                Integer.class,
                event.id()
        );
        assertThat(status).isEqualTo("DEAD");
        assertThat(deadLetters).isEqualTo(1);
    }
}
