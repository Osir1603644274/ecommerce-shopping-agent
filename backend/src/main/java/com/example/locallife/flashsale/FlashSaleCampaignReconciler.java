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
    void reconcile() {
        for (FlashSaleCampaign campaign : mapper.findActiveCampaigns()) {
            try {
                if (!gateway.isReady(campaign.id())) {
                    gateway.rebuild(campaign, mapper.findBuyerIds(campaign.id()));
                }
            } catch (RuntimeException exception) {
                log.warn("秒杀活动 Redis 状态恢复失败: campaignId={}", campaign.id(), exception);
            }
        }
    }
}
