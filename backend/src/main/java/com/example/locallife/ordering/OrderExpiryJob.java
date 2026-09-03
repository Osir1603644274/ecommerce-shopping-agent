package com.example.locallife.ordering;

import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(
        name = "local-life.order.expiry-enabled",
        havingValue = "true",
        matchIfMissing = true
)
class OrderExpiryJob {
    private final OrderService service;

    OrderExpiryJob(OrderService service) {
        this.service = service;
    }

    @Scheduled(fixedDelayString = "${local-life.order.expiry-scan-delay:PT30S}")
    void expirePendingOrders() {
        service.expireBatch(200);
    }
}
