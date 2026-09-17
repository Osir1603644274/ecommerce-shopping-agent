package com.example.locallife.flashsale;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(prefix="local-life.flash-sale", name="enabled", havingValue="true")
class FlashSaleRequestRecovery {
    private static final Logger log = LoggerFactory.getLogger(FlashSaleRequestRecovery.class);
    private final FlashSaleRequestStore requests;
    private final FlashSaleOrderPersistenceService orders;
    private final FlashSaleRedisGateway redis;
    private final FlashSaleProperties properties;
    private final ObjectMapper json;

    FlashSaleRequestRecovery(FlashSaleRequestStore requests, FlashSaleOrderPersistenceService orders,
                             FlashSaleRedisGateway redis, FlashSaleProperties properties, ObjectMapper json) {
        this.requests=requests; this.orders=orders; this.redis=redis; this.properties=properties; this.json=json;
    }

    @Scheduled(fixedDelayString="${local-life.flash-sale.recovery-delay:PT5S}")
    void recover() {
        for (var request : requests.pending()) {
            try {
                if ("COMPENSATING".equals(request.status())) {
                    redis.compensate(request.campaignId(),request.userId(),request.id());
                    orders.compensationCompleted(request.id());
                    continue;
                }
                orders.persist(request.id(),request.campaignId(),request.userId(),request.amountMinor(),
                        "recovery:"+request.id());
            } catch (RuntimeException failure) {
                try {
                    if (request.attempts()+1 >= properties.maxAttempts()) {
                        boolean compensate = orders.deadLetterOrder("recovery:"+request.id(),
                                json.writeValueAsString(request),request.attempts()+1,failure,
                                request.id(),request.campaignId(),request.userId());
                        if (compensate) {
                            redis.compensate(request.campaignId(),request.userId(),request.id());
                            orders.compensationCompleted(request.id());
                        }
                    } else {
                        requests.retry(request.id(),failure.toString());
                    }
                } catch (Exception recoveryFailure) {
                    try {
                        // Move failed compensation out of the first page as well;
                        // a Redis outage must not starve SQL-only order recovery.
                        requests.retry(request.id(),recoveryFailure.toString());
                    } catch (RuntimeException retryFailure) {
                        recoveryFailure.addSuppressed(retryFailure);
                    }
                    log.warn("Accepted flash-sale request remains recoverable: {}",request.id(),recoveryFailure);
                }
            }
        }
    }
}
