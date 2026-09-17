package com.example.locallife.flashsale;

import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import java.time.LocalDateTime;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.*;

@SpringBootTest
class FlashSalePersistenceIntegrationTests {
    @Autowired
    private FlashSaleOrderPersistenceService persistenceService;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Autowired
    private FlashSaleRequestStore requests;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    private String userId;
    private Long campaignId;
    private String deadLetterEventId;
    private String deadLetterRowId;

    @BeforeEach
    void setUp() {
        userId = UUID.randomUUID().toString();
        deadLetterEventId = "stream-" + UUID.randomUUID();
        deadLetterRowId = UUID.nameUUIDFromBytes(
                deadLetterEventId.getBytes(StandardCharsets.UTF_8)
        ).toString();
        jdbcTemplate.update("""
                INSERT INTO user_account(id, username, password_hash, enabled, token_version)
                VALUES(?, ?, 'unused', TRUE, 0)
                """, userId, "flash-" + UUID.randomUUID());
        jdbcTemplate.update("""
                INSERT INTO flash_sale_campaign(
                    item_type, item_id, title, sale_price_minor, total_stock,
                    available_stock, starts_at, ends_at, status, version
                ) VALUES(
                    'PRODUCT', 1001, '测试秒杀', 199900, 2, 2, ?, ?, 'ACTIVE', 0
                )
                """, LocalDateTime.now().minusMinutes(1), LocalDateTime.now().plusMinutes(10));
        campaignId = jdbcTemplate.queryForObject(
                "SELECT MAX(id) FROM flash_sale_campaign", Long.class);
    }

    @AfterEach
    void cleanUp() {
        jdbcTemplate.update("DELETE FROM flash_sale_request WHERE campaign_id=?",campaignId);
        jdbcTemplate.update(
                "DELETE FROM dead_letter_event WHERE source = 'FLASH_SALE' AND event_id = ?",
                deadLetterRowId
        );
        jdbcTemplate.update("DELETE FROM flash_sale_order WHERE campaign_id = ?", campaignId);
        jdbcTemplate.update("DELETE FROM flash_sale_campaign WHERE id = ?", campaignId);
        jdbcTemplate.update("DELETE FROM user_account WHERE id = ?", userId);
    }

    @Test
    void replayAndSameUserDifferentMessageOnlyConsumeDatabaseStockOnce() {
        FlashSaleOrder first = persistenceService.persist(
                UUID.randomUUID().toString(),
                campaignId,
                userId,
                199900L,
                "1-0"
        );

        FlashSaleOrder replay = persistenceService.persist(
                first.id(), campaignId, userId, 199900L, "1-0");
        FlashSaleOrder duplicateUser = persistenceService.persist(
                UUID.randomUUID().toString(), campaignId, userId, 199900L, "2-0");

        assertThat(replay.id()).isEqualTo(first.id());
        assertThat(duplicateUser.id()).isEqualTo(first.id());
        Integer available = jdbcTemplate.queryForObject(
                "SELECT available_stock FROM flash_sale_campaign WHERE id = ?",
                Integer.class,
                campaignId
        );
        assertThat(available).isEqualTo(1);
    }

    @Test
    void repeatedDeadLetterWriteIsIdempotentAndRetainsHighestAttempt() {
        persistenceService.deadLetter(
                deadLetterEventId, "{\"attempt\":8}", 8,
                new IllegalStateException("database unavailable")
        );
        persistenceService.deadLetter(
                deadLetterEventId, "{\"attempt\":9}", 9,
                new IllegalStateException("ack unavailable")
        );

        assertThat(jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM dead_letter_event WHERE source = 'FLASH_SALE' AND event_id = ?",
                Integer.class,
                deadLetterRowId
        )).isEqualTo(1);
        assertThat(jdbcTemplate.queryForObject(
                "SELECT attempts FROM dead_letter_event WHERE source = 'FLASH_SALE' AND event_id = ?",
                Integer.class,
                deadLetterRowId
        )).isEqualTo(9);
        assertThat(jdbcTemplate.queryForObject(
                "SELECT last_error FROM dead_letter_event WHERE source = 'FLASH_SALE' AND event_id = ?",
                String.class,
                deadLetterRowId
        )).isEqualTo("ack unavailable");
    }

    @Test
    void acceptedRequestRecoversWithoutRedisAndReplayConsumesStockOnlyOnce() {
        String id=UUID.randomUUID().toString();
        requests.accept(id,campaignId,userId,199900L);
        jdbcTemplate.update("UPDATE flash_sale_request SET created_at=?,next_attempt_at=? WHERE id=?",
                LocalDateTime.now(java.time.ZoneOffset.UTC).minusMinutes(10),
                LocalDateTime.now(java.time.ZoneOffset.UTC).minusMinutes(10),id);
        assertThat(requests.pending()).anyMatch(request -> request.id().equals(id));
        var redis = mock(FlashSaleRedisGateway.class);
        new FlashSaleRequestRecovery(requests,persistenceService,redis,
                new FlashSaleProperties(true,"stream.test","group",java.time.Duration.ofSeconds(1),8),
                new com.fasterxml.jackson.databind.ObjectMapper()).recover();
        verifyNoInteractions(redis);
        persistenceService.persist(id,campaignId,userId,199900L,"late-stream-message");
        assertThat(jdbcTemplate.queryForObject("SELECT available_stock FROM flash_sale_campaign WHERE id=?",Integer.class,campaignId)).isEqualTo(1);
        assertThat(jdbcTemplate.queryForObject("SELECT status FROM flash_sale_request WHERE id=?",String.class,id)).isEqualTo("COMPLETED");
    }

    @Test
    void committedOrderPreventsCompensation() {
        String id=UUID.randomUUID().toString();
        requests.accept(id,campaignId,userId,199900L);
        persistenceService.persist(id,campaignId,userId,199900L,"1-0");
        assertThat(persistenceService.deadLetterOrder(deadLetterEventId,"{}",8,new IllegalStateException(),id,campaignId,userId)).isFalse();
        assertThat(jdbcTemplate.queryForObject("SELECT status FROM flash_sale_request WHERE id=?",String.class,id)).isEqualTo("COMPLETED");
    }

    @Test
    void terminalFailureBlocksLateDeliveryAndRecoversInterruptedCompensation() {
        String id=UUID.randomUUID().toString();
        requests.accept(id,campaignId,userId,199900L);
        assertThat(persistenceService.deadLetterOrder(deadLetterEventId,"{}",8,
                new IllegalStateException("terminal"),id,campaignId,userId)).isTrue();
        assertThatThrownBy(()->persistenceService.persist(id,campaignId,userId,199900L,"late"))
                .isInstanceOf(RuntimeException.class);
        jdbcTemplate.update("UPDATE flash_sale_request SET created_at=?,next_attempt_at=? WHERE id=?",
                LocalDateTime.now(java.time.ZoneOffset.UTC).minusMinutes(1),
                LocalDateTime.now(java.time.ZoneOffset.UTC).minusMinutes(1),id);
        var redis = mock(FlashSaleRedisGateway.class);
        new FlashSaleRequestRecovery(requests,persistenceService,redis,
                new FlashSaleProperties(true,"stream.test","group",java.time.Duration.ofSeconds(1),8),
                new com.fasterxml.jackson.databind.ObjectMapper()).recover();
        verify(redis).compensate(campaignId,userId,id);
        assertThat(jdbcTemplate.queryForObject("SELECT status FROM flash_sale_request WHERE id=?",String.class,id)).isEqualTo("DEAD");
        assertThatThrownBy(()->persistenceService.persist(id,campaignId,userId,199900L,"later"))
                .isInstanceOf(RuntimeException.class);
        assertThat(jdbcTemplate.queryForObject("SELECT available_stock FROM flash_sale_campaign WHERE id=?",Integer.class,campaignId)).isEqualTo(2);
    }
}
