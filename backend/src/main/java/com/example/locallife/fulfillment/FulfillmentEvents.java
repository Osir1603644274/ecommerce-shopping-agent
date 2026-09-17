package com.example.locallife.fulfillment;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.integration.EventEnvelope;
import com.example.locallife.integration.DomainEventTypes;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.Set;

@Service
public class FulfillmentEvents {
    private static final String CONSUMER = "fulfillment-v1";
    private static final Set<String> EVENTS = Set.of("order.created.v1", "order.paid.v1",
            "order.cancelled.v1", "order.expired.v1", "order.refunded.v1", DomainEventTypes.ORDER_PARTIAL_REFUNDED_V2);
    private final FulfillmentStore store;
    @org.springframework.beans.factory.annotation.Value("${local-life.fulfillment.dispatch-delay-seconds:0}")
    private long dispatchDelaySeconds;

    FulfillmentEvents(FulfillmentStore store) { this.store = store; }

    /** All local effects and the receipt commit together. There is no asynchronous handler/ACK gap. */
    @Transactional
    public void accept(EventEnvelope event) {
        if (!EVENTS.contains(event.eventType())) return;
        if (!"ORDER".equals(event.aggregateType()) || event.id() == null || event.id().length() > 36)
            throw new IllegalArgumentException("Invalid order event identity");
        String orderStatus = store.lockOrder(event.aggregateId());
        String hash = hash(event.aggregateType() + "\n" + event.aggregateId() + "\n"
                + event.eventType() + "\n" + event.payloadJson());
        var receipts = store.jdbc().query("""
            SELECT payload_hash FROM inbox_event WHERE consumer_name=? AND event_id=? FOR UPDATE
            """, (rs, n) -> rs.getString(1), CONSUMER, event.id());
        if (!receipts.isEmpty()) {
            if (!hash.equals(receipts.get(0))) throw new BusinessConflictException("履约事件身份或负载冲突");
            return;
        }
        align(event.aggregateId(), orderStatus);
        store.jdbc().update("""
            INSERT INTO inbox_event(consumer_name,event_id,event_type,payload_hash,status,attempts,processed_at)
            VALUES(?,?,?,?,'PROCESSED',1,CURRENT_TIMESTAMP)
            """, CONSUMER, event.id(), event.eventType(), hash);
    }

    @Transactional
    public void reconcile(String orderId) {
        align(orderId, store.lockOrder(orderId));
    }

    private void align(String orderId, String orderStatus) {
        FulfillmentTask task = store.lockTask(orderId);
        if (task == null || !Set.of("WAITING_PAYMENT", "READY").contains(task.status())) return;
        if ("PAID".equals(orderStatus) && "WAITING_PAYMENT".equals(task.status())) {
            store.jdbc().update("""
                UPDATE fulfillment_task SET status='READY',next_attempt_at=?,
                updated_at=CURRENT_TIMESTAMP WHERE order_id=?
                """, java.time.LocalDateTime.now(java.time.ZoneOffset.UTC).plusSeconds(Math.max(0, dispatchDelaySeconds)), orderId);
            store.audit(orderId, task.fence(), "READY", "Authoritative paid order observed");
        } else if (Set.of("REFUNDING", "REFUNDED", "CANCELLED", "EXPIRED").contains(orderStatus)) {
            store.jdbc().update("UPDATE fulfillment_task SET status='CANCELLED',updated_at=CURRENT_TIMESTAMP WHERE order_id=?", orderId);
            store.audit(orderId, task.fence(), "CANCELLED", "Authoritative order status=" + orderStatus);
        }
    }

    static String hash(String value) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8))); }
        catch (java.security.NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }
}
