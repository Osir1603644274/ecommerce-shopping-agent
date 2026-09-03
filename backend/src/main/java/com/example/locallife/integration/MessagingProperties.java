package com.example.locallife.integration;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties("local-life.messaging")
public record MessagingProperties(
        boolean enabled,
        String transport,
        String topic,
        String redisStream,
        String consumerGroup,
        String consumerId,
        int maxAttempts,
        Duration relayDelay,
        Duration staleClaimAfter
) {
    public MessagingProperties {
        transport = transport == null || transport.isBlank() ? "kafka" : transport;
        topic = topic == null || topic.isBlank() ? "local-life.domain-events.v1" : topic;
        redisStream = redisStream == null || redisStream.isBlank()
                ? "stream.domain-events" : redisStream;
        consumerGroup = consumerGroup == null || consumerGroup.isBlank()
                ? "local-life-backend" : consumerGroup;
        consumerId = consumerId == null || consumerId.isBlank()
                ? "domain-event-projection" : consumerId;
        maxAttempts = maxAttempts <= 0 ? 8 : maxAttempts;
        relayDelay = relayDelay == null ? Duration.ofSeconds(1) : relayDelay;
        staleClaimAfter = staleClaimAfter == null ? Duration.ofMinutes(5) : staleClaimAfter;
    }
}
