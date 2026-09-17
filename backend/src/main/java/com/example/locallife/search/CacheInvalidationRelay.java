package com.example.locallife.search;

import com.example.locallife.product.ProductCache;
import com.example.locallife.shop.ShopCache;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import java.time.LocalDateTime;
import org.slf4j.LoggerFactory;

@Component
@ConditionalOnExpression("${local-life.cache.shop.enabled:true} or ${local-life.cache.product.enabled:true}")
class CacheInvalidationRelay {
    private final JdbcTemplate jdbc;
    private final StringRedisTemplate redis;
    private final RabbitTemplate rabbit;
    CacheInvalidationRelay(JdbcTemplate jdbc,StringRedisTemplate redis,RabbitTemplate rabbit) {
        this.jdbc=jdbc; this.redis=redis; this.rabbit=rabbit;
    }
    @Scheduled(fixedDelayString="${local-life.cache.invalidation-relay-delay:PT1S}")
    void relay() {
        var rows=jdbc.queryForList("SELECT id,entity_type,entity_id FROM cache_invalidation_outbox WHERE published_at IS NULL AND next_attempt_at<=CURRENT_TIMESTAMP ORDER BY next_attempt_at,created_at LIMIT 100");
        for(var row: rows) {
            String id=row.get("id").toString(), entity=row.get("entity_type").toString();
            long entityId=((Number)row.get("entity_id")).longValue();
            try {
                redis.delete("product".equals(entity)?ProductCache.key(entityId):ShopCache.detailKey(entityId));
                rabbit.invoke(operations->{
                    operations.convertAndSend(CacheInvalidationPublisher.CHANNEL,"",entity+":"+entityId);
                    operations.waitForConfirmsOrDie(5000);
                    return true;
                });
                jdbc.update("UPDATE cache_invalidation_outbox SET published_at=CURRENT_TIMESTAMP,attempts=attempts+1,last_error=NULL WHERE id=?",id);
            } catch (RuntimeException e) {
                jdbc.update("UPDATE cache_invalidation_outbox SET attempts=attempts+1,next_attempt_at=?,last_error=? WHERE id=?",
                        LocalDateTime.now().plusSeconds(5),e.toString().substring(0,Math.min(1000,e.toString().length())),id);
                LoggerFactory.getLogger(getClass()).warn("Cache invalidation will retry: {}",id,e);
            }
        }
    }
}
