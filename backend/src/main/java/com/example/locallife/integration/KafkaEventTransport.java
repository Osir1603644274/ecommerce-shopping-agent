package com.example.locallife.integration;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import java.time.Duration;

@Component
@ConditionalOnExpression(
        "${local-life.messaging.enabled:true}"
                + " and '${local-life.messaging.transport:kafka}' == 'kafka'"
)
class KafkaEventTransport implements EventTransport {
    private final KafkaTemplate<String, String> kafkaTemplate;
    private final ObjectMapper objectMapper;
    private final MessagingProperties properties;

    KafkaEventTransport(
            KafkaTemplate<String, String> kafkaTemplate,
            ObjectMapper objectMapper,
            MessagingProperties properties
    ) {
        this.kafkaTemplate = kafkaTemplate;
        this.objectMapper = objectMapper;
        this.properties = properties;
    }

    @Override
    public void publish(EventEnvelope event) {
        try {
            String value = objectMapper.writeValueAsString(event);
            kafkaTemplate.send(properties.topic(), event.aggregateId(), value)
                    .get(Duration.ofSeconds(10).toMillis(), java.util.concurrent.TimeUnit.MILLISECONDS);
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("Kafka 事件无法序列化", exception);
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Kafka 事件发送失败", exception);
        } catch (Exception exception) {
            throw new IllegalStateException("Kafka 事件发送失败", exception);
        }
    }

    @Override
    public String name() {
        return "KAFKA";
    }
}
