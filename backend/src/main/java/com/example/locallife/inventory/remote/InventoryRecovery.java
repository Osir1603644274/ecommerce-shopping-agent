package com.example.locallife.inventory.remote;

import com.example.locallife.integration.*;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

@Component
@ConditionalOnProperty(name="local-life.inventory.remote-enabled",havingValue="true")
public class InventoryRecovery implements InboundEventHandler {
    private static final Logger log=LoggerFactory.getLogger(InventoryRecovery.class);
    private final JdbcTemplate jdbc;private final InventoryCommandDelivery delivery;private final ObjectMapper json;private final boolean enabled;
    public InventoryRecovery(JdbcTemplate jdbc,InventoryCommandDelivery delivery,ObjectMapper json,
                             @Value("${local-life.inventory.recovery-enabled:true}") boolean enabled){
        this.jdbc=jdbc;this.delivery=delivery;this.json=json;this.enabled=enabled;
    }
    @Override public boolean supports(String type){return enabled && InventoryJournal.EVENT.equals(type);}
    @Override public void handle(EventEnvelope event){
        try{
            String id=json.readTree(event.payloadJson()).path("commandId").asText();
            if(id.isBlank() || !delivery.deliver(id))throw new IllegalStateException("inventory_command_not_acknowledged");
        }catch(Exception unresolved){throw new IllegalStateException("inventory_command_notification_unresolved",unresolved);}
    }
    @Scheduled(fixedDelayString="${local-life.inventory.recovery-delay-ms:2000}")
    public void recover(){
        if(!enabled)return;
        try{
            for(var row:jdbc.queryForList("SELECT command_id,order_id FROM inventory_command_journal WHERE status='TRY' AND created_at<TIMESTAMPADD(SECOND,-3,CURRENT_TIMESTAMP) ORDER BY sequence_id LIMIT 50")){
                try{delivery.reconcileTry((String)row.get("command_id"),(String)row.get("order_id"));}
                catch(RuntimeException busy){log.warn("Inventory TRY resolution deferred: {}",busy.getClass().getSimpleName());}
            }
            for(String id:jdbc.query("SELECT command_id FROM inventory_command_journal WHERE status='PENDING' AND next_attempt_at<=CURRENT_TIMESTAMP ORDER BY sequence_id LIMIT 50",(rs,n)->rs.getString(1)))
                try{delivery.deliver(id);}catch(RuntimeException busy){log.warn("Inventory command delivery deferred: {}",busy.getClass().getSimpleName());}
        }catch(RuntimeException unavailable){log.warn("Inventory recovery scan unavailable: {}",unavailable.getClass().getSimpleName());}
    }
}
