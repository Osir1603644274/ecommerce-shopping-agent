package com.example.locallife.flashsale;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties("local-life.flash-sale")
public record FlashSaleProperties(
        boolean enabled,
        String stream,
        String consumerGroup,
        Duration claimIdleAfter,
        int maxAttempts
) {
    public FlashSaleProperties {
        stream = stream == null || stream.isBlank()
                ? "stream.flash-sale-orders" : stream;
        consumerGroup = consumerGroup == null || consumerGroup.isBlank()
                ? "flash-sale-order-group" : consumerGroup;
        claimIdleAfter = claimIdleAfter == null
                ? Duration.ofMinutes(1) : claimIdleAfter;
        maxAttempts = maxAttempts <= 0 ? 8 : maxAttempts;
    }
}
