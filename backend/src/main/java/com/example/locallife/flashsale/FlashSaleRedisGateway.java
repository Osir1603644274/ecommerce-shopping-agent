package com.example.locallife.flashsale;

import org.springframework.core.io.ClassPathResource;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.List;
import java.util.UUID;

@Component
public class FlashSaleRedisGateway {
    public static final long ACCEPTED = 0L;
    public static final long SOLD_OUT = 1L;
    public static final long DUPLICATE = 2L;
    public static final long NOT_READY = 3L;
    private static final String HASH_TAG = "{flash-sale}";
    private static final DefaultRedisScript<Long> PURCHASE_SCRIPT;
    private static final DefaultRedisScript<Long> UNLOCK_SCRIPT =
            new DefaultRedisScript<>("""
                    if redis.call('GET', KEYS[1]) == ARGV[1] then
                        return redis.call('DEL', KEYS[1])
                    end
                    return 0
                    """, Long.class);
    private static final DefaultRedisScript<Long> COMPENSATE_SCRIPT =
            new DefaultRedisScript<>("""
                    if redis.call('HGET', KEYS[3], ARGV[1]) ~= ARGV[2] then
                        return 0
                    end
                    if redis.call('SREM', KEYS[2], ARGV[1]) == 1 then
                        redis.call('HDEL', KEYS[3], ARGV[1])
                        redis.call('INCR', KEYS[1])
                        return 1
                    end
                    return 0
                    """, Long.class);

    static {
        PURCHASE_SCRIPT = new DefaultRedisScript<>();
        PURCHASE_SCRIPT.setLocation(
                new ClassPathResource("scripts/flash_sale_purchase.lua"));
        PURCHASE_SCRIPT.setResultType(Long.class);
    }

    private final StringRedisTemplate redisTemplate;
    private final FlashSaleProperties properties;
    private final FlashSaleConcurrencyMetrics concurrencyMetrics;

    public FlashSaleRedisGateway(
            StringRedisTemplate redisTemplate,
            FlashSaleProperties properties,
            FlashSaleConcurrencyMetrics concurrencyMetrics
    ) {
        this.redisTemplate = redisTemplate;
        this.properties = properties;
        this.concurrencyMetrics = concurrencyMetrics;
    }

    public long purchase(
            Long campaignId,
            String orderId,
            String userId,
            long amountMinor
    ) {
        Long result;
        try (var ignored = concurrencyMetrics.enterRedisScriptCall()) {
            result = redisTemplate.execute(
                    PURCHASE_SCRIPT,
                    List.of(
                            stockKey(campaignId),
                            buyersKey(campaignId),
                            readyKey(campaignId),
                            reservationKey(campaignId)
                    ),
                    orderId,
                    userId,
                    campaignId.toString(),
                    Long.toString(amountMinor)
            );
        }
        if (result == null) {
            throw new IllegalStateException("Redis 秒杀脚本没有返回结果");
        }
        com.example.locallife.diagnostics.BackendTrace.mark("redis_lua", "秒杀原子预扣", switch(result.intValue()) {
            case 0 -> "accepted"; case 1 -> "sold_out"; case 2 -> "duplicate"; case 3 -> "not_ready"; default -> "unknown";
        });
        return result;
    }

    public void rebuild(FlashSaleCampaign campaign, List<String> buyerIds) {
        rebuild(campaign, buyerIds, java.util.Map.of());
    }

    void rebuild(FlashSaleCampaign campaign, List<String> buyerIds, java.util.Map<String,String> reservations) {
        String token = UUID.randomUUID().toString();
        String lockKey = rebuildLockKey(campaign.id());
        Boolean locked = redisTemplate.opsForValue()
                .setIfAbsent(lockKey, token, Duration.ofSeconds(30));
        if (!Boolean.TRUE.equals(locked)) {
            return;
        }
        try {
            redisTemplate.delete(readyKey(campaign.id()));
            redisTemplate.delete(stockKey(campaign.id()));
            redisTemplate.delete(buyersKey(campaign.id()));
            redisTemplate.delete(reservationKey(campaign.id()));
            redisTemplate.opsForValue().set(
                    stockKey(campaign.id()),
                    campaign.availableStock().toString()
            );
            if (!buyerIds.isEmpty()) {
                redisTemplate.opsForSet().add(
                        buyersKey(campaign.id()),
                        buyerIds.toArray(String[]::new)
                );
            }
            if (!reservations.isEmpty()) {
                redisTemplate.opsForHash().putAll(reservationKey(campaign.id()), reservations);
            }
            redisTemplate.opsForValue().set(readyKey(campaign.id()), "1");
        } finally {
            redisTemplate.execute(UNLOCK_SCRIPT, List.of(lockKey), token);
        }
    }

    public boolean isReady(Long campaignId) {
        return Boolean.TRUE.equals(redisTemplate.hasKey(readyKey(campaignId)));
    }

    public void compensate(Long campaignId, String userId, String orderId) {
        redisTemplate.execute(
                COMPENSATE_SCRIPT,
                List.of(stockKey(campaignId), buyersKey(campaignId), reservationKey(campaignId)),
                userId, orderId
        );
    }

    private static String reservationKey(Long campaignId) {
        return "flash:" + HASH_TAG + ":campaign:" + campaignId + ":reservations";
    }

    private static String stockKey(Long campaignId) {
        return "flash:" + HASH_TAG + ":campaign:" + campaignId + ":stock";
    }

    private static String buyersKey(Long campaignId) {
        return "flash:" + HASH_TAG + ":campaign:" + campaignId + ":buyers";
    }

    private static String readyKey(Long campaignId) {
        return "flash:" + HASH_TAG + ":campaign:" + campaignId + ":ready";
    }

    private static String rebuildLockKey(Long campaignId) {
        return "flash:" + HASH_TAG + ":campaign:" + campaignId + ":rebuild-lock";
    }
}
