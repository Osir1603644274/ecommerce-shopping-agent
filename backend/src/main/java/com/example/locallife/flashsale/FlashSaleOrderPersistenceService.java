package com.example.locallife.flashsale;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.ResourceNotFoundException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
class FlashSaleOrderPersistenceService {
    private final FlashSaleMapper mapper;
    private final FlashSaleRequestStore requests;

    FlashSaleOrderPersistenceService(FlashSaleMapper mapper, FlashSaleRequestStore requests) {
        this.mapper = mapper;
        this.requests = requests;
    }

    @Transactional
    FlashSaleOrder persist(
            String orderId,
            Long campaignId,
            String userId,
            Long amountMinor,
            String streamMessageId
    ) {
        // The same lock is held by terminal-failure handling, so a late delivery
        // cannot commit an order after its reservation was released.
        FlashSaleCampaign campaign = mapper.lockCampaign(campaignId);
        if (campaign == null) throw new ResourceNotFoundException("秒杀活动不存在");
        if (!campaign.salePriceMinor().equals(amountMinor))
            throw new BusinessConflictException("秒杀消息金额与活动价格不一致");
        requests.assertNotDead(orderId);
        FlashSaleOrder existing = mapper.findOrder(orderId);
        if (existing != null) {
            if (!existing.campaignId().equals(campaignId) || !existing.userId().equals(userId))
                throw new BusinessConflictException("秒杀消息订单身份冲突");
            requests.complete(orderId, existing.id());
            return existing;
        }
        existing = mapper.findOrderByCampaignAndUser(campaignId, userId);
        if (existing != null) {
            requests.complete(orderId, existing.id());
            return existing;
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
        requests.complete(orderId, orderId);
        return mapper.findOrder(orderId);
    }

    @Transactional
    public boolean deadLetterOrder(String messageId, String payload, int attempts, Throwable failure,
                                   String orderId, Long campaignId, String userId) {
        mapper.lockCampaign(campaignId);
        var existing = mapper.findOrderByCampaignAndUser(campaignId, userId);
        if (existing != null) {
            requests.complete(orderId, existing.id());
            return false;
        }
        deadLetter(messageId, payload, attempts, failure);
        requests.dead(orderId, failure.getClass().getSimpleName());
        return true;
    }

    @Transactional
    public void compensationCompleted(String orderId) { requests.compensated(orderId); }

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
