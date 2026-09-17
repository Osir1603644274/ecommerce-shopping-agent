package com.example.locallife.inventory.remote;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.inventory.*;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import java.time.LocalDateTime;
import java.util.List;

@Service
@ConditionalOnProperty(name="local-life.inventory.remote-enabled",havingValue="true")
public class DistributedInventory {
    private final RemoteInventoryClient client;private final InventoryJournal journal;
    public DistributedInventory(RemoteInventoryClient client,InventoryJournal journal){this.client=client;this.journal=journal;}
    public InventoryStock stock(String type,Long id){return client.stock(type,id);}
    public InventoryStock createStock(String type,Long id,int quantity){return client.createStock(type,id,quantity);}
    public InventoryReservation reserve(String order,String type,Long item,int quantity,LocalDateTime expires){
        if(!TransactionSynchronizationManager.isActualTransactionActive())throw new IllegalStateException("order_transaction_required_before_try");
        var command=new StockCommand("reserve:"+order+":"+type+":"+item,order,"RESERVE",List.of(new StockCommand.Item(type,item,quantity)),expires);
        journal.prepareTry(command);
        var receipt=client.apply(command);journal.observeTry(command,receipt);
        if(!"APPLIED".equals(receipt.path("status").asText()))throw new BusinessConflictException("库存预占被拒绝："+receipt.path("reason").asText());
        var stock=client.stock(type,item);
        for(var row:receipt.path("reservations"))if(row.path("stockId").asLong()==stock.id())
            return new InventoryReservation(row.path("id").asText(),order,stock.id(),quantity,row.path("status").asText(),expires);
        throw new IllegalStateException("reservation_receipt_missing_item");
    }
    public void enqueue(String order,String kind){
        journal.enqueue(new StockCommand(kind.toLowerCase(java.util.Locale.ROOT)+":"+order,order,kind,List.of(),null));
    }
    public void refund(String order,String refundId,String type,Long item,int quantity){
        if(refundId==null || refundId.isBlank())throw new IllegalArgumentException("stable_refund_identity_required");
        journal.enqueue(new StockCommand("refund:"+refundId+":"+item,order,"REFUND",List.of(new StockCommand.Item(type,item,quantity)),null));
    }
}
