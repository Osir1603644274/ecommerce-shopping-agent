package com.example.locallife.integration;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.LocalDateTime;
import java.util.UUID;
import java.nio.charset.StandardCharsets;
import org.springframework.dao.DuplicateKeyException;

@Service
public class OutboxService {
    private final OutboxMapper mapper;
    private final ObjectMapper objectMapper;
    private final Clock clock;

    @Autowired
    public OutboxService(OutboxMapper mapper, ObjectMapper objectMapper) {
        this(mapper, objectMapper, Clock.systemUTC());
    }

    OutboxService(OutboxMapper mapper, ObjectMapper objectMapper, Clock clock) {
        this.mapper = mapper;
        this.objectMapper = objectMapper;
        this.clock = clock;
    }

    @Transactional
    public String append(
            String aggregateType,
            String aggregateId,
            String eventType,
            Object payload
    ) {
        LocalDateTime now = LocalDateTime.now(clock);
        String id = UUID.randomUUID().toString();
        mapper.insert(new OutboxEvent(
                id,
                requireText(aggregateType, "aggregateType"),
                requireText(aggregateId, "aggregateId"),
                requireText(eventType, "eventType"),
                serialize(payload),
                "PENDING",
                0,
                now,
                null,
                null,
                null,
                null,
                now
        ));
        return id;
    }

    @Transactional
    public String appendIdempotent(
            String idempotencyKey,
            String aggregateType,
            String aggregateId,
            String eventType,
            Object payload
    ) {
        String key = requireText(idempotencyKey, "idempotencyKey");
        String id = UUID.nameUUIDFromBytes(key.getBytes(StandardCharsets.UTF_8)).toString();
        LocalDateTime now = LocalDateTime.now(clock);
        String payloadJson = serialize(payload);
        OutboxEvent event = new OutboxEvent(
                id,
                requireText(aggregateType, "aggregateType"),
                requireText(aggregateId, "aggregateId"),
                requireText(eventType, "eventType"),
                payloadJson,
                "PENDING", 0, now, null, null, null, null, now
        );
        try {
            mapper.insert(event);
        } catch (DuplicateKeyException duplicate) {
            OutboxEvent existing = mapper.findById(id);
            if (existing == null
                    || !event.aggregateType().equals(existing.aggregateType())
                    || !event.aggregateId().equals(existing.aggregateId())
                    || !event.eventType().equals(existing.eventType())
                    || !event.payloadJson().equals(existing.payloadJson())) {
                throw new IllegalStateException("Outbox idempotency key conflict: " + key, duplicate);
            }
        }
        return id;
    }

    private String serialize(Object payload) {
        try {
            return objectMapper.writeValueAsString(payload);
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("业务事件无法序列化", exception);
        }
    }

    private static String requireText(String value, String field) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(field + " 不能为空");
        }
        return value.strip();
    }
}
