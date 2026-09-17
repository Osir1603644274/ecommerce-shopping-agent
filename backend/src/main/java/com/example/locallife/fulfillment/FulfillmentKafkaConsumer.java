package com.example.locallife.fulfillment;

import com.example.locallife.integration.EventEnvelope;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(prefix = "local-life.fulfillment", name = "kafka-enabled", havingValue = "true")
class FulfillmentKafkaConsumer {
    private final FulfillmentEvents events;
    private final ObjectMapper json;

    FulfillmentKafkaConsumer(FulfillmentEvents events, ObjectMapper json) { this.events = events; this.json = json; }

    @KafkaListener(topics = "${local-life.messaging.topic:local-life.domain-events.v1}",
            groupId = "${local-life.fulfillment.consumer-group:local-life-fulfillment-v1}",
            containerFactory = "fulfillmentKafkaListenerContainerFactory")
    void consume(String value, Acknowledgment acknowledgment) throws com.fasterxml.jackson.core.JsonProcessingException {
        events.accept(json.readValue(value, EventEnvelope.class));
        acknowledgment.acknowledge();
    }
}
