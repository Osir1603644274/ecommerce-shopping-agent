package com.example.locallife.flashsale;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;

@Mapper
interface FlashSaleMapper {
    String CAMPAIGN_COLUMNS = """
            id, item_type, item_id, title, sale_price_minor, total_stock,
            available_stock, starts_at, ends_at, status, version, created_at, updated_at
            """;

    @Insert("""
            INSERT INTO flash_sale_campaign(
                item_type, item_id, title, sale_price_minor, total_stock,
                available_stock, starts_at, ends_at, status, version
            ) VALUES(
                #{campaign.itemType}, #{campaign.itemId}, #{campaign.title},
                #{campaign.salePriceMinor}, #{campaign.totalStock}, #{campaign.totalStock},
                #{campaign.startsAt}, #{campaign.endsAt}, 'DRAFT', 0
            )
            """)
    @Options(useGeneratedKeys = true, keyProperty = "campaign.id")
    int insertCampaign(@Param("campaign") MutableCampaign campaign);

    @Select("SELECT " + CAMPAIGN_COLUMNS + " FROM flash_sale_campaign WHERE id = #{id}")
    FlashSaleCampaign findCampaign(@Param("id") Long id);

    @Select("SELECT " + CAMPAIGN_COLUMNS
            + " FROM flash_sale_campaign ORDER BY starts_at DESC")
    List<FlashSaleCampaign> findCampaigns();

    @Select("SELECT " + CAMPAIGN_COLUMNS
            + " FROM flash_sale_campaign WHERE status = 'ACTIVE'")
    List<FlashSaleCampaign> findActiveCampaigns();

    @Update("""
            UPDATE flash_sale_campaign
            SET status = 'ACTIVE', version = version + 1
            WHERE id = #{id} AND status = 'DRAFT'
            """)
    int activate(@Param("id") Long id);

    @Update("""
            UPDATE flash_sale_campaign
            SET available_stock = available_stock - 1, version = version + 1
            WHERE id = #{id} AND status = 'ACTIVE' AND available_stock > 0
            """)
    int decrementDatabaseStock(@Param("id") Long id);

    @Select("""
            SELECT id, campaign_id, user_id, status, amount_minor,
                   stream_message_id, created_at, updated_at
            FROM flash_sale_order WHERE id = #{id}
            """)
    FlashSaleOrder findOrder(@Param("id") String id);

    @Select("""
            SELECT id, campaign_id, user_id, status, amount_minor,
                   stream_message_id, created_at, updated_at
            FROM flash_sale_order
            WHERE campaign_id = #{campaignId} AND user_id = #{userId}
            """)
    FlashSaleOrder findOrderByCampaignAndUser(
            @Param("campaignId") Long campaignId,
            @Param("userId") String userId
    );

    @Select("""
            SELECT id, campaign_id, user_id, status, amount_minor,
                   stream_message_id, created_at, updated_at
            FROM flash_sale_order
            WHERE user_id = #{userId}
            ORDER BY created_at DESC
            """)
    List<FlashSaleOrder> findOrdersByUser(@Param("userId") String userId);

    @Select("""
            SELECT user_id FROM flash_sale_order WHERE campaign_id = #{campaignId}
            """)
    List<String> findBuyerIds(@Param("campaignId") Long campaignId);

    @Insert("""
            INSERT INTO flash_sale_order(
                id, campaign_id, user_id, status, amount_minor, stream_message_id
            ) VALUES(
                #{order.id}, #{order.campaignId}, #{order.userId}, #{order.status},
                #{order.amountMinor}, #{order.streamMessageId}
            )
            """)
    int insertOrder(@Param("order") FlashSaleOrder order);

    @Insert("""
            INSERT INTO dead_letter_event(
                source, event_id, event_type, payload_json, attempts, last_error
            ) VALUES(
                'FLASH_SALE', #{eventId}, 'flash-sale.order.persist.v1',
                #{payloadJson}, #{attempts}, #{error}
            )
            ON DUPLICATE KEY UPDATE
                payload_json = VALUES(payload_json),
                attempts = GREATEST(attempts, VALUES(attempts)),
                last_error = VALUES(last_error),
                failed_at = CURRENT_TIMESTAMP
            """)
    int insertDeadLetter(
            @Param("eventId") String eventId,
            @Param("payloadJson") String payloadJson,
            @Param("attempts") int attempts,
            @Param("error") String error
    );

    final class MutableCampaign {
        private Long id;
        private final String itemType;
        private final Long itemId;
        private final String title;
        private final Long salePriceMinor;
        private final Integer totalStock;
        private final LocalDateTime startsAt;
        private final LocalDateTime endsAt;

        MutableCampaign(CreateFlashSaleCampaignRequest request) {
            itemType = request.itemType();
            itemId = request.itemId();
            title = request.title().strip();
            salePriceMinor = request.salePriceMinor();
            totalStock = request.totalStock();
            startsAt = LocalDateTime.ofInstant(
                    request.startsAt().toInstant(), ZoneOffset.UTC);
            endsAt = LocalDateTime.ofInstant(
                    request.endsAt().toInstant(), ZoneOffset.UTC);
        }

        public Long getId() { return id; }
        public void setId(Long id) { this.id = id; }
        public String getItemType() { return itemType; }
        public Long getItemId() { return itemId; }
        public String getTitle() { return title; }
        public Long getSalePriceMinor() { return salePriceMinor; }
        public Integer getTotalStock() { return totalStock; }
        public LocalDateTime getStartsAt() { return startsAt; }
        public LocalDateTime getEndsAt() { return endsAt; }
    }
}
