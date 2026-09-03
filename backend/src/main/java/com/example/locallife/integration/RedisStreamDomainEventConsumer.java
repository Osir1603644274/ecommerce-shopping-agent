package com.example.locallife.integration;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.data.redis.connection.stream.Consumer;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.connection.stream.ReadOffset;
import org.springframework.data.redis.connection.stream.StreamOffset;
import org.springframework.data.redis.connection.stream.StreamReadOptions;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.List;

@Component
@ConditionalOnExpression(
        "${local-life.messaging.enabled:true}"
                + " and '${local-life.messaging.transport:kafka}' == 'redis-stream'"
)
class RedisStreamDomainEventConsumer {
    private static final Logger log = LoggerFactory.getLogger(RedisStreamDomainEventConsumer.class);
    private final StringRedisTemplate redisTemplate;
    private final ObjectMapper objectMapper;
    private final InboundEventProcessor processor;
    private final MessagingProperties properties;
    private final String consumerName;

    RedisStreamDomainEventConsumer(
            StringRedisTemplate redisTemplate,
            ObjectMapper objectMapper,
            InboundEventProcessor processor,
            MessagingProperties properties,
            @Value("${HOSTNAME:local}") String consumerName
    ) {
        this.redisTemplate = redisTemplate;
        this.objectMapper = objectMapper;
        this.processor = processor;
        this.properties = properties;
        this.consumerName = consumerName;
    }

    @PostConstruct
    void createGroup() {
        try {
            if (Boolean.FALSE.equals(redisTemplate.hasKey(properties.redisStream()))) {
                redisTemplate.opsForStream().add(
                        properties.redisStream(),
                        java.util.Map.of("bootstrap", "true")
                );
            }
            redisTemplate.opsForStream().createGroup(
                    properties.redisStream(),
                    ReadOffset.from("0-0"),
                    properties.consumerGroup()
            );
        } catch (RuntimeException exception) {
            if (!isBusyGroup(exception)) {
                log.warn("Redis Stream 消费组初始化失败，后续轮询将重试", exception);
            }
        }
    }

    @Scheduled(fixedDelayString = "${local-life.messaging.redis-poll-delay:PT1S}")
    void poll() {
        try {
            List<MapRecord<String, Object, Object>> records = redisTemplate.opsForStream().read(
                    Consumer.from(properties.consumerGroup(), consumerName),
                    StreamReadOptions.empty().count(20).block(Duration.ofMillis(500)),
                    StreamOffset.create(properties.redisStream(), ReadOffset.lastConsumed())
            );
            if (records == null) {
                return;
            }
            for (MapRecord<String, Object, Object> record : records) {
                Object envelope = record.getValue().get("envelope");
                if (envelope == null) {
                    redisTemplate.opsForStream().acknowledge(
                            properties.redisStream(),
                            properties.consumerGroup(),
                            record.getId()
                    );
                    continue;
                }
                processor.process(
                        properties.consumerId(),
                        deserialize(envelope.toString())
                );
                redisTemplate.opsForStream().acknowledge(
                        properties.redisStream(),
                        properties.consumerGroup(),
                        record.getId()
                );
            }
        } catch (RuntimeException exception) {
            log.warn("Redis Stream 事件轮询失败", exception);
            createGroup();
        }
    }

    private EventEnvelope deserialize(String value) {
        try {
            return objectMapper.readValue(value, EventEnvelope.class);
        } catch (Exception exception) {
            throw new IllegalArgumentException("Redis Stream 事件信封不是合法 JSON", exception);
        }
    }

    private static boolean isBusyGroup(RuntimeException exception) {
        return exception.getMessage() != null
                && exception.getMessage().contains("BUSYGROUP");
    }
}
