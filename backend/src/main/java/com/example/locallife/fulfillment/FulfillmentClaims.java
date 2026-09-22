package com.example.locallife.fulfillment;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

@Service
public class FulfillmentClaims {
    private final FulfillmentStore store;
    private final FulfillmentProperties properties;
    private final ObjectMapper json;
    private com.example.locallife.inventory.InventorySettlement inventoryJournal;

    @org.springframework.beans.factory.annotation.Autowired(required=false)
    public void setInventoryJournal(com.example.locallife.inventory.InventorySettlement journal) {this.inventoryJournal=journal;}

    FulfillmentClaims(FulfillmentStore store, FulfillmentProperties properties, ObjectMapper json) {
        this.store = store; this.properties = properties; this.json = json;
    }

    public List<String> candidates(int limit) {
        return store.jdbc().query("""
            SELECT order_id FROM fulfillment_task
            WHERE ((status IN ('READY','UNKNOWN') AND next_attempt_at &lt;= CURRENT_TIMESTAMP(6))
               OR (status='DISPATCHING' AND lease_until &lt;= CURRENT_TIMESTAMP(6)))
            ORDER BY next_attempt_at,order_id LIMIT ?
            """.replace("&lt;", "<"), (rs, n) -> rs.getString(1), Math.max(1, Math.min(limit, 100)));
    }

    @Transactional
    public Optional<FulfillmentTask> claim(String orderId, String owner) {
        String orderStatus = store.lockOrder(orderId);
        FulfillmentTask task = store.lockTask(orderId);
        // next_attempt_at and lease_until are TIMESTAMP(6); compare at the same precision on MySQL.
        LocalDateTime now = store.jdbc().queryForObject("SELECT CURRENT_TIMESTAMP(6)", LocalDateTime.class);
        if (task == null) return Optional.empty();
        // Money/order state alone does not prove that remote inventory has caught up.
        // Do not dispatch while confirm/refund effects are unresolved or need review.
        if(inventoryJournal!=null && !inventoryJournal.settled(orderId))return Optional.empty();
        boolean due = (List.of("READY", "UNKNOWN").contains(task.status()) && task.nextAttemptAt() != null
                && !task.nextAttemptAt().isAfter(now))
                || ("DISPATCHING".equals(task.status()) && task.leaseUntil() != null && !task.leaseUntil().isAfter(now));
        if (!due) return Optional.empty();
        if (!"PAID".equals(orderStatus)) {
            store.jdbc().update("UPDATE fulfillment_task SET status='NEEDS_REVIEW',last_error=?,updated_at=? WHERE order_id=?",
                    "Order no longer PAID: " + orderStatus, now, orderId);
            return Optional.empty();
        }
        store.jdbc().update("""
            UPDATE fulfillment_task SET status='DISPATCHING',owner=?,fence=fence+1,attempts=attempts+1,
            lease_until=?,updated_at=? WHERE order_id=?
            """, owner, now.plus(properties.lease()), now, orderId);
        FulfillmentTask claimed = store.lockTask(orderId);
        store.audit(orderId, claimed.fence(), "CLAIMED", owner);
        return Optional.of(claimed);
    }

    @Transactional
    public boolean shipped(FulfillmentTask claim, WarehouseReceipt receipt) {
        if (!claim.requestKey().equals(receipt.requestKey()) || !claim.orderId().equals(receipt.orderId())
                || !FulfillmentEvents.hash(claim.commandJson()).equals(receipt.commandHash())
                || receipt.trackingNo() == null || receipt.trackingNo().isBlank() || receipt.trackingNo().length() > 128)
            throw new IllegalArgumentException("Warehouse receipt identity does not match immutable command");
        store.lockOrder(claim.orderId());
        try {
            int rows = store.jdbc().update("""
                UPDATE fulfillment_task SET status='SHIPPED',tracking_no=?,receipt_json=?,last_error=NULL,
                owner=NULL,lease_until=NULL,next_attempt_at=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE order_id=? AND status='DISPATCHING' AND owner=? AND fence=?
                """, receipt.trackingNo(), json.writeValueAsString(receipt), claim.orderId(), claim.owner(), claim.fence());
            if (rows == 1) store.audit(claim.orderId(), claim.fence(), "SHIPPED", receipt.trackingNo());
            return rows == 1;
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) { throw new IllegalStateException(e); }
    }

    @Transactional
    public boolean failed(FulfillmentTask claim, String detail) {
        store.lockOrder(claim.orderId());
        boolean exhausted = claim.attempts() >= properties.maxAttempts();
        String error = detail == null ? "Unknown warehouse result" : detail.substring(0, Math.min(1000, detail.length()));
        int rows = store.jdbc().update("""
            UPDATE fulfillment_task SET status=?,last_error=?,next_attempt_at=?,owner=NULL,lease_until=NULL,
            updated_at=CURRENT_TIMESTAMP WHERE order_id=? AND status='DISPATCHING' AND owner=? AND fence=?
            """, exhausted ? "NEEDS_REVIEW" : "UNKNOWN", error,
                exhausted ? null : store.jdbc().queryForObject("SELECT CURRENT_TIMESTAMP(6)", LocalDateTime.class)
                        .plusSeconds(Math.min(300, 1L << Math.min(8, claim.attempts()))),
                claim.orderId(), claim.owner(), claim.fence());
        if (rows == 1) store.audit(claim.orderId(), claim.fence(), exhausted ? "NEEDS_REVIEW" : "UNKNOWN", error);
        return rows == 1;
    }
}
