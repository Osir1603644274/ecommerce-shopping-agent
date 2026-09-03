package com.example.locallife.integration;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.connection.stream.StreamRecords;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.util.Map;

@Component
@ConditionalOnExpression(
        "${local-life.messaging.enabled:true}"
                + " and '${local-life.messaging.transport:kafka}' == 'redis-stream'"
)
class RedisStreamEventTransport implements EventTransport {
    private final StringRedisTemplate redisTemplate;
    private final ObjectMapper objectMapper;
    private final MessagingProperties properties;

    RedisStreamEventTransport(
            StringRedisTemplate redisTemplate,
            ObjectMapper objectMapper,
            MessagingProperties properties
    ) {
        this.redisTemplate = redisTemplate;
        this.objectMapper = objectMapper;
        this.properties = properties;
    }

    @Override
    public void publish(EventEnvelope event) {
        try {
            MapRecord<String, String, String> record = StreamRecords
                    .newRecord()
                    .in(properties.redisStream())
                    .ofMap(Map.of(
                            "eventId", event.id(),
                            "eventType", event.eventType(),
                            "aggregateId", event.aggregateId(),
                            "envelope", objectMapper.writeValueAsString(event)
                    ));
            redisTemplate.opsForStream().add(record);
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("Redis Stream 事件无法序列化", exception);
        }
    }

    @Override
    public String name() {
        return "REDIS_STREAM";
    }
}
