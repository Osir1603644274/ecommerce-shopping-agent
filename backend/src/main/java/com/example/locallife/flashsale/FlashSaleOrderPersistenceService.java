package com.example.locallife.flashsale;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.ResourceNotFoundException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
class FlashSaleOrderPersistenceService {
    private final FlashSaleMapper mapper;

    FlashSaleOrderPersistenceService(FlashSaleMapper mapper) {
        this.mapper = mapper;
    }

    @Transactional
    FlashSaleOrder persist(
            String orderId,
            Long campaignId,
            String userId,
            Long amountMinor,
            String streamMessageId
    ) {
        FlashSaleOrder existing = mapper.findOrder(orderId);
        if (existing != null) {
            return existing;
        }
        existing = mapper.findOrderByCampaignAndUser(campaignId, userId);
        if (existing != null) {
            return existing;
        }
        FlashSaleCampaign campaign = mapper.findCampaign(campaignId);
        if (campaign == null) {
            throw new ResourceNotFoundException("秒杀活动不存在");
        }
        if (!campaign.salePriceMinor().equals(amountMinor)) {
            throw new BusinessConflictException("秒杀消息金额与活动价格不一致");
        }
        if (mapper.decrementDatabaseStock(campaignId) != 1) {
            throw new BusinessConflictException("数据库秒杀库存不足或活动已关闭");
        }
        FlashSaleOrder order = new FlashSaleOrder(
                orderId,
                campaignId,
                userId,
                "PENDING_PAYMENT",
                amountMinor,
                streamMessageId,
                null,
                null
        );
        mapper.insertOrder(order);
        return mapper.findOrder(orderId);
    }

    @Transactional
    void deadLetter(
            String streamMessageId,
            String payloadJson,
            int attempts,
            Throwable failure
    ) {
        String error = failure.getMessage() == null
                ? failure.getClass().getSimpleName()
                : failure.getMessage();
        if (error.length() > 1000) {
            error = error.substring(0, 1000);
        }
        mapper.insertDeadLetter(
                java.util.UUID.nameUUIDFromBytes(
                        streamMessageId.getBytes(java.nio.charset.StandardCharsets.UTF_8)
                ).toString(),
                payloadJson,
                attempts,
                error
        );
    }
}
