package com.example.locallife.integration;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnExpression(
        "${local-life.messaging.enabled:true}"
                + " and '${local-life.messaging.transport:kafka}' == 'kafka'"
)
class KafkaDomainEventConsumer {
    private final ObjectMapper objectMapper;
    private final InboundEventProcessor processor;
    private final MessagingProperties properties;

    KafkaDomainEventConsumer(
            ObjectMapper objectMapper,
            InboundEventProcessor processor,
            MessagingProperties properties
    ) {
        this.objectMapper = objectMapper;
        this.processor = processor;
        this.properties = properties;
    }

    @KafkaListener(
            topics = "${local-life.messaging.topic:local-life.domain-events.v1}",
            groupId = "${local-life.messaging.consumer-group:local-life-backend}"
    )
    void consume(String value, Acknowledgment acknowledgment) {
        EventEnvelope event = deserialize(value);
        processor.process(properties.consumerId(), event);
        acknowledgment.acknowledge();
    }

    private EventEnvelope deserialize(String value) {
        try {
            return objectMapper.readValue(value, EventEnvelope.class);
        } catch (Exception exception) {
            throw new IllegalArgumentException("Kafka 事件信封不是合法 JSON", exception);
        }
    }
}
