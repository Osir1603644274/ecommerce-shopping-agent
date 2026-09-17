package com.example.locallife.fulfillment;

import com.example.locallife.integration.EventEnvelope;
import com.example.locallife.common.ResourceNotFoundException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.UUID;

@Service
public class FulfillmentDeadLetters {
    private final FulfillmentStore store;
    private final ObjectMapper json;
    private final FulfillmentEvents events;
    FulfillmentDeadLetters(FulfillmentStore store, ObjectMapper json, FulfillmentEvents events) {
        this.store = store; this.json = json; this.events = events;
    }

    @Transactional
    public void record(String topic, int partition, long offset, String value, Exception failure) {
        String id = UUID.nameUUIDFromBytes((topic + "/" + partition + "/" + offset).getBytes(StandardCharsets.UTF_8)).toString();
        try {
            String payload = json.writeValueAsString(Map.of("topic", topic, "partition", partition, "offset", offset, "value", value));
            store.jdbc().update("""
                INSERT INTO dead_letter_event(source,event_id,event_type,payload_json,attempts,last_error)
                VALUES('FULFILLMENT_KAFKA',?,'fulfillment.delivery.failed',?,4,?)
                ON DUPLICATE KEY UPDATE attempts=attempts+1
                """, id, payload, failure.getClass().getSimpleName());
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) { throw new IllegalStateException(e); }
    }

    @Transactional
    public void replay(String id, String actor) {
        String raw = store.jdbc().query("""
            SELECT payload_json FROM dead_letter_event WHERE source='FULFILLMENT_KAFKA' AND event_id=? FOR UPDATE
            """, (rs, n) -> rs.getString(1), id).stream().findFirst()
                .orElseThrow(() -> new ResourceNotFoundException("履约死信不存在"));
        try {
            var payload = json.readTree(raw);
            // MySQL JSON is an object; H2's JSON test column can expose a JSON string containing the object.
            if (payload.isTextual()) payload = json.readTree(payload.asText());
            EventEnvelope event = json.readValue(payload.path("value").asText(), EventEnvelope.class);
            events.accept(event);
            if (store.find(event.aggregateId()) != null)
                store.audit(event.aggregateId(), 0, "EVENT_REPLAY", "actor=" + actor + "; deadLetter=" + id);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new org.springframework.web.server.ResponseStatusException(org.springframework.http.HttpStatus.CONFLICT,
                    "原始事件仍不可解析，不能重放", e);
        }
    }
}
