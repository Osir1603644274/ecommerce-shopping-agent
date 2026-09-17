package com.example.locallife.flashsale;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(
        prefix = "local-life.flash-sale",
        name = "enabled",
        havingValue = "true"
)
class FlashSaleCampaignReconciler {
    private static final Logger log = LoggerFactory.getLogger(FlashSaleCampaignReconciler.class);
    private final FlashSaleMapper mapper;
    private final FlashSaleRedisGateway gateway;

    FlashSaleCampaignReconciler(
            FlashSaleMapper mapper,
            FlashSaleRedisGateway gateway
    ) {
        this.mapper = mapper;
        this.gateway = gateway;
    }

    @Scheduled(fixedDelayString = "${local-life.flash-sale.reconcile-delay:PT30S}")
    @org.springframework.transaction.annotation.Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public void reconcile() {
        for (FlashSaleCampaign campaign : mapper.findActiveCampaigns()) {
            try {
                if (!gateway.isReady(campaign.id())) {
                    campaign = mapper.lockCampaign(campaign.id());
                    var reservations = mapper.reservations(campaign.id()).stream().collect(
                            java.util.stream.Collectors.toMap(FlashSaleMapper.Reservation::userId,FlashSaleMapper.Reservation::id));
                    var restored = new FlashSaleCampaign(campaign.id(),campaign.itemType(),campaign.itemId(),
                            campaign.title(),campaign.salePriceMinor(),campaign.totalStock(),
                            Math.max(0,campaign.availableStock()-reservations.size()),
                            campaign.startsAt(),campaign.endsAt(),campaign.status(),campaign.version(),
                            campaign.createdAt(),campaign.updatedAt());
                    gateway.rebuild(restored, mapper.findReservedBuyerIds(campaign.id()),reservations);
                }
            } catch (RuntimeException exception) {
                log.warn("秒杀活动 Redis 状态恢复失败: campaignId={}", campaign.id(), exception);
            }
        }
    }
}
