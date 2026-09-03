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

@SpringBootTest
class FlashSalePersistenceIntegrationTests {
    @Autowired
    private FlashSaleOrderPersistenceService persistenceService;

    @Autowired
    private JdbcTemplate jdbcTemplate;

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
}
