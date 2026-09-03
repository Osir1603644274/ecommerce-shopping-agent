package com.example.locallife.flashsale;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.common.ResourceNotFoundException;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;

@Service
public class FlashSaleService {
    private final FlashSaleMapper mapper;
    private final FlashSaleRedisGateway redisGateway;
    private final FlashSaleProperties properties;
    private final Clock clock;

    @Autowired
    public FlashSaleService(
            FlashSaleMapper mapper,
            FlashSaleRedisGateway redisGateway,
            FlashSaleProperties properties
    ) {
        this(mapper, redisGateway, properties, Clock.systemUTC());
    }

    FlashSaleService(
            FlashSaleMapper mapper,
            FlashSaleRedisGateway redisGateway,
            FlashSaleProperties properties,
            Clock clock
    ) {
        this.mapper = mapper;
        this.redisGateway = redisGateway;
        this.properties = properties;
        this.clock = clock;
    }

    @Transactional
    public FlashSaleCampaign create(CreateFlashSaleCampaignRequest request) {
        if (!request.endsAt().toInstant().isAfter(request.startsAt().toInstant())) {
            throw new InvalidBusinessStateException("秒杀结束时间必须晚于开始时间");
        }
        FlashSaleMapper.MutableCampaign mutable =
                new FlashSaleMapper.MutableCampaign(request);
        mapper.insertCampaign(mutable);
        return requireCampaign(mutable.getId());
    }

    @Transactional
    public FlashSaleCampaign activate(Long campaignId) {
        FlashSaleCampaign campaign = requireCampaign(campaignId);
        if ("ACTIVE".equals(campaign.status())) {
            return campaign;
        }
        if (!"DRAFT".equals(campaign.status())) {
            throw new InvalidBusinessStateException("只有草稿活动可以启用");
        }
        redisGateway.rebuild(campaign, List.of());
        if (mapper.activate(campaignId) != 1) {
            throw new BusinessConflictException("秒杀活动状态已变化");
        }
        return requireCampaign(campaignId);
    }

    public FlashSalePurchaseResponse purchase(Long campaignId, String userId) {
        if (!properties.enabled()) {
            throw new InvalidBusinessStateException("秒杀能力未启用");
        }
        FlashSaleCampaign campaign = requireCampaign(campaignId);
        LocalDateTime now = LocalDateTime.now(clock);
        if (!"ACTIVE".equals(campaign.status())) {
            throw new InvalidBusinessStateException("秒杀活动未启用");
        }
        if (now.isBefore(campaign.startsAt())) {
            throw new InvalidBusinessStateException("秒杀活动尚未开始");
        }
        if (!now.isBefore(campaign.endsAt())) {
            throw new InvalidBusinessStateException("秒杀活动已经结束");
        }
        String orderId = UUID.randomUUID().toString();
        long result = redisGateway.purchase(
                campaignId,
                orderId,
                userId,
                campaign.salePriceMinor()
        );
        if (result == FlashSaleRedisGateway.SOLD_OUT) {
            throw new BusinessConflictException("秒杀商品已售罄");
        }
        if (result == FlashSaleRedisGateway.DUPLICATE) {
            throw new BusinessConflictException("同一用户只能购买一次");
        }
        if (result == FlashSaleRedisGateway.NOT_READY) {
            throw new InvalidBusinessStateException("秒杀库存正在恢复，请稍后重试");
        }
        if (result != FlashSaleRedisGateway.ACCEPTED) {
            throw new IllegalStateException("未知秒杀脚本结果: " + result);
        }
        return new FlashSalePurchaseResponse(
                orderId,
                "QUEUED",
                "抢购请求已进入可靠队列"
        );
    }

    public List<FlashSaleCampaign> listCampaigns() {
        return mapper.findCampaigns();
    }

    public List<FlashSaleOrder> listMine(String userId) {
        return mapper.findOrdersByUser(userId);
    }

    FlashSaleCampaign requireCampaign(Long id) {
        FlashSaleCampaign campaign = mapper.findCampaign(id);
        if (campaign == null) {
            throw new ResourceNotFoundException("秒杀活动不存在");
        }
        return campaign;
    }
}
