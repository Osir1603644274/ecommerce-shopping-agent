package com.example.inventoryservice;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.server.ResponseStatusException;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.*;
import static com.example.inventoryservice.InventoryProtocol.*;

/** Inventory-only transaction. Caller transactions cannot roll back these effects. */
@Service
public class InventoryCommands {
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final TransactionTemplate tx;
    public InventoryCommands(JdbcTemplate jdbc,ObjectMapper json,PlatformTransactionManager manager){
        this.jdbc=jdbc;this.json=json;this.tx=new TransactionTemplate(manager);
        tx.setTimeout(5);
    }
    private static Stock stockRow(ResultSet rs,int row) throws SQLException {
        return new Stock(rs.getLong("id"),rs.getString("item_type"),rs.getLong("item_id"),
            rs.getInt("total_quantity"),rs.getInt("available_quantity"),rs.getInt("reserved_quantity"),
            rs.getInt("sold_quantity"),rs.getLong("version"));
    }
    public Optional<Stock> stock(String type,Long id){
        return jdbc.query("SELECT * FROM inventory_stock WHERE item_type=? AND item_id=?",InventoryCommands::stockRow,type,id).stream().findFirst();
    }
    /** Natural-key creation is replay-safe; this endpoint is not a restock operation. */
    public Stock createStock(Item item){
        return tx.execute(status->{
            jdbc.update("""
                INSERT INTO inventory_stock(item_type,item_id,total_quantity,available_quantity)
                VALUES(?,?,?,?) ON DUPLICATE KEY UPDATE id=id
                """,item.itemType(),item.itemId(),item.quantity(),item.quantity());
            Stock value=stock(item.itemType(),item.itemId()).orElseThrow();
            if(value.totalQuantity()!=item.quantity())
                throw new ResponseStatusException(HttpStatus.CONFLICT,"stock_creation_quantity_conflict");
            return value;
        });
    }
    public List<Stock> stocks(StockQuery query){
        var ids=query.itemIds().stream().distinct().toList();
        var args=new ArrayList<Object>();args.add(query.itemType());args.addAll(ids);
        String placeholders=String.join(",",Collections.nCopies(ids.size(),"?"));
        return jdbc.query("SELECT * FROM inventory_stock WHERE item_type=? AND item_id IN ("+placeholders+") ORDER BY item_id",InventoryCommands::stockRow,args.toArray());
    }
    public Optional<Receipt> receipt(String id){
        return jdbc.query("SELECT response_json FROM inventory_command_receipt WHERE command_id=? AND status<>'PROCESSING'",
            (rs,n)->decode(rs.getString(1)),id).stream().findFirst();
    }
    private Receipt decode(String text){
        try{return json.readValue(text,Receipt.class);}catch(Exception error){throw new IllegalStateException("invalid_persisted_inventory_receipt",error);}
    }
    private String encode(Receipt receipt){
        try{return json.writeValueAsString(receipt);}catch(Exception error){throw new IllegalStateException(error);}
    }
    private List<Reservation> reservations(String order){
        return jdbc.query("SELECT * FROM inventory_reservation WHERE order_id=? ORDER BY stock_id",
            (rs,n)->new Reservation(rs.getString("id"),rs.getString("order_id"),rs.getLong("stock_id"),
                rs.getInt("quantity"),rs.getInt("refunded_quantity"),rs.getString("status"),
                rs.getTimestamp("expires_at").toLocalDateTime()),order);
    }
    public Receipt apply(Command input){
        Command command=input.normalized();String hash=command.hash();
        return tx.execute(status->{
            // The primary-key write serializes retries even across different service instances.
            jdbc.update("""
                INSERT INTO inventory_command_receipt(command_id,order_id,request_hash,kind,status)
                VALUES(?,?,?,?,'PROCESSING') ON DUPLICATE KEY UPDATE command_id=command_id
                """,command.commandId(),command.orderId(),hash,command.kind());
            var row=jdbc.queryForMap("SELECT * FROM inventory_command_receipt WHERE command_id=? FOR UPDATE",command.commandId());
            if(!hash.equals(row.get("request_hash")) || !command.orderId().equals(row.get("order_id")))
                throw new ResponseStatusException(HttpStatus.CONFLICT,"inventory_command_key_conflict");
            if(!"PROCESSING".equals(row.get("status")))return decode(String.valueOf(row.get("response_json")));
            // Backfill compatibility for pre-migration reservations, without reading trade tables.
            var prior=reservations(command.orderId());
            String initial=prior.stream().anyMatch(r->Set.of("CONFIRMED","REFUNDED","RETURNED").contains(r.status()))?"CONFIRMED":
                prior.stream().anyMatch(r->Set.of("RELEASED","EXPIRED").contains(r.status()))?"RELEASED":"OPEN";
            jdbc.update("INSERT INTO inventory_order_guard(order_id,state) VALUES(?,?) ON DUPLICATE KEY UPDATE order_id=order_id",command.orderId(),initial);
            String guard=jdbc.queryForObject("SELECT state FROM inventory_order_guard WHERE order_id=? FOR UPDATE",String.class,command.orderId());
            Object savepoint=status.createSavepoint();
            String result="APPLIED",reason="ok";
            try{execute(command,guard);}
            catch(Rejected rejected){
                // Reject atomically: no partially reserved multi-SKU cart, but keep its receipt.
                status.rollbackToSavepoint(savepoint);result="REJECTED";reason=rejected.getMessage();
            } finally {status.releaseSavepoint(savepoint);}
            Receipt receipt=new Receipt(command.commandId(),command.orderId(),hash,result,reason,reservations(command.orderId()));
            jdbc.update("UPDATE inventory_command_receipt SET status=?,response_json=? WHERE command_id=?",result,encode(receipt),command.commandId());
            return receipt;
        });
    }
    private void execute(Command command,String guard){
        switch(command.kind()){
            case "RESERVE" -> reserve(command,guard);
            case "CONFIRM" -> transition(command,guard,true);
            case "RELEASE" -> transition(command,guard,false);
            case "REFUND","RESTORE_ALL" -> refund(command,guard);
            case "RETURN_SELLABLE","RETURN_QUARANTINE" -> receiveReturn(command,guard);
            default -> throw new IllegalArgumentException("unsupported_inventory_command");
        }
    }
    private void reserve(Command command,String guard){
        if(!"OPEN".equals(guard))throw new Rejected("order_inventory_terminal");
        var stocks=new ArrayList<Map.Entry<Stock,Item>>();
        for(Item item:command.items())stocks.add(Map.entry(stock(item.itemType(),item.itemId()).orElseThrow(()->new Rejected("stock_not_configured")),item));
        stocks.sort(Comparator.comparing(entry->entry.getKey().id()));
        for(var entry:stocks){
            var stock=entry.getKey();var item=entry.getValue();
            var previous=reservations(command.orderId()).stream().filter(r->r.stockId().equals(stock.id())).findFirst();
            if(previous.isPresent()){
                if(previous.get().quantity()!=item.quantity() || !"RESERVED".equals(previous.get().status()))throw new Rejected("reservation_parameter_conflict");
                continue;
            }
            if(jdbc.update("""
                UPDATE inventory_stock SET available_quantity=available_quantity-?,reserved_quantity=reserved_quantity+?,version=version+1
                WHERE id=? AND available_quantity>=?
                """,item.quantity(),item.quantity(),stock.id(),item.quantity())!=1)throw new Rejected("insufficient_stock");
            jdbc.update("""
                INSERT INTO inventory_reservation(id,order_id,stock_id,quantity,status,expires_at)
                VALUES(?,?,?,?,'RESERVED',?)
                """,UUID.randomUUID().toString(),command.orderId(),stock.id(),item.quantity(),command.expiresAt());
        }
    }
    private void transition(Command command,String guard,boolean confirm){
        String target=confirm?"CONFIRMED":"RELEASED";
        if(target.equals(guard))return;
        if(!"OPEN".equals(guard))throw new Rejected("inventory_terminal_conflict");
        var rows=reservations(command.orderId());
        if(confirm && rows.isEmpty())throw new Rejected("reservation_not_found");
        for(var reservation:rows){
            if(!"RESERVED".equals(reservation.status()))throw new Rejected("reservation_state_conflict");
            String sql=confirm?
                "UPDATE inventory_stock SET reserved_quantity=reserved_quantity-?,sold_quantity=sold_quantity+?,version=version+1 WHERE id=? AND reserved_quantity>=?":
                "UPDATE inventory_stock SET reserved_quantity=reserved_quantity-?,available_quantity=available_quantity+?,version=version+1 WHERE id=? AND reserved_quantity>=?";
            int quantity=reservation.quantity();
            if(jdbc.update(sql,quantity,quantity,reservation.stockId(),quantity)!=1)throw new Rejected("inventory_balance_conflict");
            jdbc.update("UPDATE inventory_reservation SET status=? WHERE id=?",target,reservation.id());
        }
        jdbc.update("UPDATE inventory_order_guard SET state=? WHERE order_id=?",target,command.orderId());
    }
    private void refund(Command command,String guard){
        if(!"CONFIRMED".equals(guard))throw new Rejected("inventory_not_confirmed");
        var rows=reservations(command.orderId());
        var quantities=new TreeMap<Long,Integer>();
        if("RESTORE_ALL".equals(command.kind())){
            if(jdbc.queryForObject("SELECT COALESCE(SUM(returned_quantity),0) FROM inventory_reservation WHERE order_id=?",Integer.class,command.orderId())>0)
                throw new Rejected("physical_return_already_received");
            for(var row:rows)if(row.quantity()>row.refundedQuantity())quantities.put(row.stockId(),row.quantity()-row.refundedQuantity());
        }else for(var item:command.items())
            quantities.put(stock(item.itemType(),item.itemId()).orElseThrow(()->new Rejected("stock_not_configured")).id(),item.quantity());
        for(var entry:quantities.entrySet()){
            var row=rows.stream().filter(r->r.stockId().equals(entry.getKey())).findFirst().orElseThrow(()->new Rejected("reservation_not_found"));
            int quantity=entry.getValue();
            int returned=jdbc.queryForObject("SELECT returned_quantity FROM inventory_reservation WHERE id=?",Integer.class,row.id());
            if(!"CONFIRMED".equals(row.status()) || (long)row.refundedQuantity()+returned+quantity>row.quantity())throw new Rejected("refund_quantity_exceeded");
            if(jdbc.update("UPDATE inventory_stock SET sold_quantity=sold_quantity-?,available_quantity=available_quantity+?,version=version+1 WHERE id=? AND sold_quantity>=?",
                quantity,quantity,row.stockId(),quantity)!=1)throw new Rejected("inventory_balance_conflict");
            jdbc.update("UPDATE inventory_reservation SET refunded_quantity=refunded_quantity+?,status=? WHERE id=?",
                quantity,row.refundedQuantity()+returned+quantity==row.quantity()?"REFUNDED":"CONFIRMED",row.id());
        }
    }
    private void receiveReturn(Command command,String guard){
        if(!"CONFIRMED".equals(guard))throw new Rejected("inventory_not_confirmed");
        Item item=command.items().get(0);
        Stock stock=stock(item.itemType(),item.itemId()).orElseThrow(()->new Rejected("stock_not_configured"));
        int quantity=item.quantity();boolean sellable="RETURN_SELLABLE".equals(command.kind());
        if(jdbc.update("""
            UPDATE inventory_reservation SET status=CASE WHEN returned_quantity+refunded_quantity+?=quantity THEN 'RETURNED' ELSE status END,
              returned_quantity=returned_quantity+?
            WHERE order_id=? AND stock_id=? AND status='CONFIRMED' AND returned_quantity+refunded_quantity+?<=quantity
            """,quantity,quantity,command.orderId(),stock.id(),quantity)!=1)throw new Rejected("return_quantity_exceeded");
        String sql=sellable?
            "UPDATE inventory_stock SET sold_quantity=sold_quantity-?,available_quantity=available_quantity+?,version=version+1 WHERE id=? AND sold_quantity>=?":
            "UPDATE inventory_stock SET sold_quantity=sold_quantity-?,total_quantity=total_quantity-?,version=version+1 WHERE id=? AND sold_quantity>=? AND total_quantity>=?";
        int count=sellable?jdbc.update(sql,quantity,quantity,stock.id(),quantity):jdbc.update(sql,quantity,quantity,stock.id(),quantity,quantity);
        if(count!=1)throw new Rejected("inventory_balance_conflict");
        jdbc.update("INSERT INTO inventory_return_receipt(command_id,order_id,item_id,stock_id,quantity,disposition) VALUES(?,?,?,?,?,?)",
            command.commandId(),command.orderId(),item.itemId(),stock.id(),quantity,command.kind());
    }
    private static class Rejected extends RuntimeException{Rejected(String reason){super(reason);}}
}
