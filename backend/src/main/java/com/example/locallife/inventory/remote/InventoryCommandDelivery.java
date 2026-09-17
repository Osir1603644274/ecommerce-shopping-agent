package com.example.locallife.inventory.remote;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.*;
import java.util.List;

@Service
@ConditionalOnProperty(name="local-life.inventory.remote-enabled",havingValue="true")
public class InventoryCommandDelivery {
    private final JdbcTemplate jdbc;private final InventoryJournal journal;private final RemoteInventoryClient client;private final ObjectMapper json;
    public InventoryCommandDelivery(JdbcTemplate jdbc,InventoryJournal journal,RemoteInventoryClient client,ObjectMapper json){
        this.jdbc=jdbc;this.journal=journal;this.client=client;this.json=json;
    }
    @Transactional(propagation=Propagation.REQUIRES_NEW,timeout=6)
    public boolean deliver(String commandId){
        var rows=jdbc.queryForList("SELECT * FROM inventory_command_journal WHERE command_id=? FOR UPDATE",commandId);
        if(rows.isEmpty())return true;
        var row=rows.get(0);String state=(String)row.get("status");
        if("ACK".equals(state) || "CANCELLED".equals(state))return true;
        if(!"PENDING".equals(state))return false;
        // Preserve each order's effect order (confirm before refund), across instances.
        int previous=jdbc.queryForObject("""
            SELECT COUNT(*) FROM inventory_command_journal WHERE order_id=? AND sequence_id<? AND status NOT IN ('ACK','CANCELLED')
            """,Integer.class,row.get("order_id"),row.get("sequence_id"));
        if(previous>0)return false;
        StockCommand command=journal.decode(String.valueOf(row.get("command_json")));
        try{
            JsonNode receipt=client.apply(command);
            String result="APPLIED".equals(receipt.path("status").asText())?"ACK":"NEEDS_REVIEW";
            jdbc.update("UPDATE inventory_command_journal SET status=?,response_json=?,attempts=attempts+1,last_error=? WHERE command_id=?",
                result,journal.encode(receipt),"ACK".equals(result)?null:receipt.path("reason").asText(),commandId);
            return "ACK".equals(result);
        }catch(RuntimeException unavailable){
            jdbc.update("""
                UPDATE inventory_command_journal SET attempts=attempts+1,next_attempt_at=TIMESTAMPADD(SECOND,LEAST(60,POW(2,LEAST(attempts,5))),CURRENT_TIMESTAMP),
                    last_error='Inventory outcome unresolved; retry original command' WHERE command_id=?
                """,commandId);
            return false;
        }
    }
    @Transactional(propagation=Propagation.REQUIRES_NEW,timeout=6)
    public void reconcileTry(String commandId,String orderId){
        // Never treat an uncommitted order as absent. This locking read waits for its
        // INSERT/transaction to finish. Timeout/deadlock is retried, not compensated.
        var orders=jdbc.queryForList("SELECT status FROM customer_order WHERE id=? FOR UPDATE",orderId);
        var rows=jdbc.queryForList("SELECT * FROM inventory_command_journal WHERE command_id=? AND status='TRY' FOR UPDATE",commandId);
        if(rows.isEmpty())return;
        if(orders.isEmpty()){
            jdbc.update("UPDATE inventory_command_journal SET status='CANCELLED' WHERE command_id=?",commandId);
            // A terminal release is safe even if the original RESERVE has not arrived.
            journal.enqueue(new StockCommand("release:"+orderId,orderId,"RELEASE",List.of(),null));
            return;
        }
        try{
            var receipt=json.readTree(String.valueOf(rows.get(0).get("response_json")));
            if(!"APPLIED".equals(receipt.path("status").asText()))throw new IllegalStateException("unproven_reservation");
            jdbc.update("UPDATE inventory_command_journal SET status='ACK' WHERE command_id=?",commandId);
        }catch(Exception unproven){
            jdbc.update("UPDATE inventory_command_journal SET status='NEEDS_REVIEW',last_error='Committed order lacks applied reservation receipt' WHERE command_id=?",commandId);
        }
    }
}
