package com.example.locallife.inventory.remote;

import com.example.locallife.integration.OutboxService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.*;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import java.util.Map;

@Service
@ConditionalOnProperty(name="local-life.inventory.remote-enabled",havingValue="true")
public class InventoryJournal implements com.example.locallife.inventory.InventorySettlement {
    public static final String EVENT="inventory.command.requested.v1";
    private final JdbcTemplate jdbc;private final ObjectMapper json;private final OutboxService outbox;private final InventoryIntentStore intents;
    public InventoryJournal(JdbcTemplate jdbc,ObjectMapper json,OutboxService outbox,InventoryIntentStore intents){this.jdbc=jdbc;this.json=json;this.outbox=outbox;this.intents=intents;}
    String encode(Object value){try{return json.writeValueAsString(value);}catch(Exception error){throw new IllegalStateException(error);}}
    StockCommand decode(String text){try{return json.readValue(text,StockCommand.class);}catch(Exception error){throw new IllegalStateException(error);}}
    private void insert(JdbcTemplate connection,StockCommand command,String state){
        connection.update("""
            INSERT INTO inventory_command_journal(command_id,order_id,kind,request_hash,command_json,status)
            VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE command_id=command_id
            """,command.commandId(),command.orderId(),command.kind(),command.hash(),encode(command),state);
        String existing=connection.queryForObject("SELECT request_hash FROM inventory_command_journal WHERE command_id=?",String.class,command.commandId());
        if(!command.hash().equals(existing))throw new IllegalStateException("inventory_journal_key_conflict");
    }
    /** Independent durability before crossing the network, even if the order rolls back. */
    public void prepareTry(StockCommand command){intents.write(connection->insert(connection,command,"TRY"));}
    /** Must join the authoritative trade commit; rollback means no command is published. */
    @Transactional(propagation=Propagation.MANDATORY)
    public void enqueue(StockCommand command){
        insert(jdbc,command,"PENDING");
        outbox.appendIdempotent("inventory-notify:"+command.commandId(),"INVENTORY",command.orderId(),EVENT,Map.of("commandId",command.commandId()));
    }
    public void observeTry(StockCommand command,Object receipt){
        intents.write(connection->connection.update("UPDATE inventory_command_journal SET response_json=? WHERE command_id=? AND status='TRY'",encode(receipt),command.commandId()));
    }
    public boolean settled(String orderId){
        return jdbc.queryForObject("SELECT COUNT(*) FROM inventory_command_journal WHERE order_id=? AND status NOT IN ('ACK','CANCELLED')",Integer.class,orderId)==0;
    }
}
