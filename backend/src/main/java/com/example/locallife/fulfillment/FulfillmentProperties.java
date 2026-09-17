package com.example.locallife.fulfillment;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;
import java.net.URI;
import java.time.Duration;

@ConfigurationProperties("local-life.fulfillment")
public record FulfillmentProperties(
        @DefaultValue("false") boolean enabled,
        @DefaultValue("false") boolean workerEnabled,
        @DefaultValue("false") boolean kafkaEnabled,
        @DefaultValue("http://127.0.0.1:19091") URI warehouseUrl,
        @DefaultValue("") String warehouseToken,
        @DefaultValue("PT2S") Duration requestTimeout,
        @DefaultValue("PT30S") Duration lease,
        @DefaultValue("2") int workers,
        @DefaultValue("16") int queueSize,
        @DefaultValue("8") int maxAttempts) {
    public FulfillmentProperties {
        if (workers < 1 || workers > 16 || queueSize < 1 || queueSize > 1000 || maxAttempts < 1
                || requestTimeout.isNegative() || requestTimeout.isZero()
                || lease.compareTo(requestTimeout.multipliedBy(3)) < 0)
            throw new IllegalArgumentException("Invalid fulfillment worker bounds");
        if ((workerEnabled || kafkaEnabled) && !enabled)
            throw new IllegalArgumentException("Fulfillment must be enabled before enabling its workers");
        if (workerEnabled && (warehouseToken == null || warehouseToken.isBlank()))
            throw new IllegalArgumentException("Warehouse token is required for fulfillment worker");
    }
}
