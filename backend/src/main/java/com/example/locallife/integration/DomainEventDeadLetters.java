package com.example.locallife.integration;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.UUID;

/** Stores the complete broker delivery even when its envelope cannot be parsed. */
@Service
public class DomainEventDeadLetters {
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final MessagingProperties properties;

    DomainEventDeadLetters(JdbcTemplate jdbc, ObjectMapper json, MessagingProperties properties) {
        this.jdbc = jdbc;
        this.json = json;
        this.properties = properties;
    }

    @Transactional
    public void record(String topic, int partition, long offset, String key, String value, Exception failure) {
        String consumerGroup = properties.consumerGroup();
        for (Throwable cause=failure; cause!=null; cause=cause.getCause()) {
            if (cause instanceof org.springframework.kafka.listener.ListenerExecutionFailedException listener
                    && listener.getGroupId()!=null) {
                consumerGroup=listener.getGroupId();
                break;
            }
        }
        String identity = consumerGroup + "/" + topic + "/" + partition + "/" + offset;
        String id = UUID.nameUUIDFromBytes(identity.getBytes(StandardCharsets.UTF_8)).toString();
        var payload = new LinkedHashMap<String, Object>();
        payload.put("topic", topic);
        payload.put("partition", partition);
        payload.put("offset", offset);
        payload.put("consumerGroup", consumerGroup);
        payload.put("key", key);
        payload.put("value", value);
        try {
            jdbc.update("""
                    INSERT INTO dead_letter_event(source,event_id,event_type,payload_json,attempts,last_error)
                    VALUES('DOMAIN_KAFKA',?,'domain.delivery.failed',?,1,?)
                    ON DUPLICATE KEY UPDATE attempts=attempts+1
                    """, id, json.writeValueAsString(payload), failure.getClass().getSimpleName());
        } catch (com.fasterxml.jackson.core.JsonProcessingException exception) {
            throw new IllegalStateException("Cannot preserve original broker delivery", exception);
        }
        // SQL failures deliberately propagate: the broker must retain the delivery.
    }
}
