package com.example.locallife.integration;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import java.time.LocalDateTime;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest(properties = "local-life.messaging.max-attempts=1")
class OutboxInboxIntegrationTests {
    @Autowired
    private OutboxService outboxService;

    @Autowired
    private OutboxMapper outboxMapper;

    @Autowired
    private OutboxClaimService outboxClaims;

    @Autowired
    private InboxClaimService inboxClaims;

    @Autowired
    private PlatformTransactionManager transactionManager;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    @AfterEach
    void cleanUp() {
        jdbcTemplate.update("DELETE FROM dead_letter_event");
        jdbcTemplate.update("DELETE FROM inbox_event");
        jdbcTemplate.update("DELETE FROM outbox_event");
    }

    @Test
    void outboxAppendParticipatesInCallerTransaction() {
        TransactionTemplate transaction = new TransactionTemplate(transactionManager);
        assertThatThrownBy(() -> transaction.executeWithoutResult(status -> {
            outboxService.append("ORDER", "rollback-order", "order.test.v1", Map.of("x", 1));
            throw new IllegalStateException("force rollback");
        })).isInstanceOf(IllegalStateException.class);

        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM outbox_event WHERE aggregate_id = 'rollback-order'",
                Integer.class
        );
        assertThat(count).isZero();
    }

    @Test
    void outboxMovesToDeadLetterAfterConfiguredAttempts() {
        String eventId = outboxService.append(
                "ORDER", "order-dead", "order.test.v1", Map.of("x", 1));
        String owner = "relay-test";
        OutboxEvent claimed = outboxClaims.claim(eventId, owner).orElseThrow();

        outboxClaims.failed(claimed, owner, new IllegalStateException("broker unavailable"));

        assertThat(outboxMapper.findById(eventId).status()).isEqualTo("DEAD");
        Integer deadLetters = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM dead_letter_event WHERE event_id = ?",
                Integer.class,
                eventId
        );
        assertThat(deadLetters).isEqualTo(1);
    }

    @Test
    void inboxDeduplicatesReplayAndRejectsPayloadCollision() {
        EventEnvelope original = new EventEnvelope(
                "event-1",
                "REVIEW",
                "review-1",
                DomainEventTypes.REVIEW_VECTOR_DELETE_V1,
                "{\"reviewId\":\"review-1\"}",
                LocalDateTime.now()
        );

        assertThat(inboxClaims.claim("consumer-a", original)).isTrue();
        inboxClaims.processed("consumer-a", original.id());
        assertThat(inboxClaims.claim("consumer-a", original)).isFalse();

        EventEnvelope collision = new EventEnvelope(
                original.id(),
                original.aggregateType(),
                original.aggregateId(),
                original.eventType(),
                "{\"reviewId\":\"different\"}",
                original.occurredAt()
        );
        assertThatThrownBy(() -> inboxClaims.claim("consumer-a", collision))
                .isInstanceOf(BusinessConflictException.class)
                .hasMessageContaining("负载不一致");
    }
}
