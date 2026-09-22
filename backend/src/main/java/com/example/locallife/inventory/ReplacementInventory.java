package com.example.locallife.inventory;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.inventory.remote.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.*;
import java.time.LocalDateTime;
import java.util.*;

/** Replacement identity is distinct from the original paid order. No payment is created. */
@Service
public class ReplacementInventory {
    public enum Result { PENDING, RESERVED, SHORTAGE, REVIEW }
    public enum ReleaseResult { PENDING, RELEASED, REVIEW }
    private final JdbcTemplate jdbc;private InventoryJournal remote;
    public ReplacementInventory(JdbcTemplate jdbc) {this.jdbc=jdbc;}
    @Autowired(required=false) public void setRemote(InventoryJournal value) {remote=value;}
    @Transactional(propagation=Propagation.MANDATORY)
    public ReleaseResult releaseExpired(String replacement) {
        if(remote!=null) {
            if(jdbc.queryForObject("SELECT COUNT(*) FROM inventory_command_journal WHERE command_id=? AND status<>'CANCELLED'",Integer.class,"replacement-dispatch:"+replacement)>0)
                return ReleaseResult.REVIEW;
            String key="replacement-release:"+replacement;
            remote.enqueue(new StockCommand(key,replacement,"RELEASE",List.of(),null));
            String state=jdbc.queryForObject("SELECT status FROM inventory_command_journal WHERE command_id=?",String.class,key);
            return "ACK".equals(state)?ReleaseResult.RELEASED:"NEEDS_REVIEW".equals(state)?ReleaseResult.REVIEW:ReleaseResult.PENDING;
        }
        var rows=jdbc.queryForList("SELECT stock_id,quantity,status FROM inventory_reservation WHERE order_id=? FOR UPDATE",replacement);
        if(rows.size()!=1) return ReleaseResult.REVIEW;
        var row=rows.get(0);
        if(java.util.Set.of("EXPIRED","RELEASED").contains(row.get("status"))) return ReleaseResult.RELEASED;
        if(!"RESERVED".equals(row.get("status"))) return ReleaseResult.REVIEW;
        int quantity=((Number)row.get("quantity")).intValue();
        if(jdbc.update("UPDATE inventory_stock SET reserved_quantity=reserved_quantity-?,available_quantity=available_quantity+?,version=version+1 WHERE id=? AND reserved_quantity>=?",
                quantity,quantity,row.get("stock_id"),quantity)!=1) throw new BusinessConflictException("补发释放库存余额不一致");
        jdbc.update("UPDATE inventory_reservation SET status='EXPIRED' WHERE order_id=?",replacement);
        return ReleaseResult.RELEASED;
    }
    @Transactional(propagation=Propagation.MANDATORY)
    public boolean dispatch(String replacement) {
        if(remote!=null) {
            String key="replacement-dispatch:"+replacement;
            remote.enqueue(new StockCommand(key,replacement,"CONFIRM",List.of(),null));
            String state=jdbc.queryForObject("SELECT status FROM inventory_command_journal WHERE command_id=?",String.class,key);
            return "ACK".equals(state);
        }
        var row=jdbc.queryForMap("SELECT stock_id,quantity,status FROM inventory_reservation WHERE order_id=? FOR UPDATE",replacement);
        if("CONFIRMED".equals(row.get("status"))) return true;
        if(!"RESERVED".equals(row.get("status"))) throw new BusinessConflictException("补发库存状态不允许出库");
        int quantity=((Number)row.get("quantity")).intValue();
        if(jdbc.update("UPDATE inventory_stock SET reserved_quantity=reserved_quantity-?,sold_quantity=sold_quantity+?,version=version+1 WHERE id=? AND reserved_quantity>=?",
                quantity,quantity,row.get("stock_id"),quantity)!=1) throw new BusinessConflictException("补发库存余额不一致");
        jdbc.update("UPDATE inventory_reservation SET status='CONFIRMED' WHERE order_id=?",replacement);
        return true;
    }
    @Transactional(propagation=Propagation.MANDATORY)
    public Result reserve(String replacement,long item,int quantity,LocalDateTime until) {
        if(quantity<=0) throw new IllegalArgumentException("positive replacement quantity required");
        if(remote!=null) {
            String key="replacement-reserve:"+replacement;
            remote.enqueue(new StockCommand(key,replacement,"RESERVE",List.of(new StockCommand.Item("PRODUCT",item,quantity)),until));
            var row=jdbc.queryForMap("SELECT status,last_error FROM inventory_command_journal WHERE command_id=?",key);
            return switch((String)row.get("status")) {
                case "ACK" -> Result.RESERVED;
                case "NEEDS_REVIEW" -> "insufficient_stock".equals(row.get("last_error"))?Result.SHORTAGE:Result.REVIEW;
                default -> Result.PENDING;
            };
        }
        var stock=jdbc.queryForMap("SELECT id FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=? FOR UPDATE",item);
        long stockId=((Number)stock.get("id")).longValue();
        var existing=jdbc.queryForList("SELECT stock_id,quantity,status FROM inventory_reservation WHERE order_id=?",replacement);
        if(!existing.isEmpty()) {
            var row=existing.get(0);
            if(((Number)row.get("stock_id")).longValue()!=stockId || ((Number)row.get("quantity")).intValue()!=quantity)
                throw new BusinessConflictException("换货库存预占参数冲突");
            return "RESERVED".equals(row.get("status"))?Result.RESERVED:Result.REVIEW;
        }
        if(jdbc.update("UPDATE inventory_stock SET available_quantity=available_quantity-?,reserved_quantity=reserved_quantity+?,version=version+1 WHERE id=? AND available_quantity>=?",
                quantity,quantity,stockId,quantity)!=1) return Result.SHORTAGE;
        jdbc.update("INSERT INTO inventory_reservation(id,order_id,stock_id,quantity,status,expires_at) VALUES(?,?,?,?,'RESERVED',?)",
                UUID.randomUUID().toString(),replacement,stockId,quantity,until);
        return Result.RESERVED;
    }
}
