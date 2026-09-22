package com.example.locallife.inventory;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.inventory.remote.InventoryJournal;
import com.example.locallife.inventory.remote.StockCommand;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.*;
import java.util.List;

/** Physical receipt accounting, independent of payment refunds. Caller holds the trade order lock. */
@Service
public class ReturnedInventory {
    private final JdbcTemplate jdbc;
    private InventoryJournal remote;
    public ReturnedInventory(JdbcTemplate jdbc) { this.jdbc=jdbc; }
    @Autowired(required=false) public void setRemote(InventoryJournal remote) { this.remote=remote; }

    @Transactional(propagation=Propagation.MANDATORY)
    public boolean receive(String commandId,String orderId,long itemId,int quantity,boolean sellable) {
        if(quantity<=0) throw new IllegalArgumentException("positive returned quantity required");
        String kind=sellable?"RETURN_SELLABLE":"RETURN_QUARANTINE";
        if(remote!=null) {
            var command=new StockCommand(commandId,orderId,kind,List.of(new StockCommand.Item("PRODUCT",itemId,quantity)),null);
            remote.enqueue(command);
            String state=jdbc.queryForObject("SELECT status FROM inventory_command_journal WHERE command_id=?",String.class,commandId);
            if("NEEDS_REVIEW".equals(state)) throw new BusinessConflictException("退货库存回执被拒绝，需核实");
            return "ACK".equals(state);
        }
        var replay=jdbc.queryForList("SELECT order_id,item_id,quantity,disposition FROM inventory_return_receipt WHERE command_id=?",commandId);
        if(!replay.isEmpty()) {
            var row=replay.get(0);
            if(!orderId.equals(row.get("order_id")) || ((Number)row.get("item_id")).longValue()!=itemId
                    || ((Number)row.get("quantity")).intValue()!=quantity || !kind.equals(row.get("disposition")))
                throw new BusinessConflictException("退货库存命令参数冲突");
            return true;
        }
        var stock=jdbc.queryForMap("SELECT id FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=? FOR UPDATE",itemId);
        long stockId=((Number)stock.get("id")).longValue();
        if(jdbc.update("""
            UPDATE inventory_reservation SET status=CASE WHEN returned_quantity+refunded_quantity+?=quantity THEN 'RETURNED' ELSE status END,
              returned_quantity=returned_quantity+?
            WHERE order_id=? AND stock_id=? AND status='CONFIRMED' AND returned_quantity+refunded_quantity+?<=quantity
            """,quantity,quantity,orderId,stockId,quantity)!=1)
            throw new BusinessConflictException("退货数量超过该订单剩余可接收数量");
        String sql=sellable?
                "UPDATE inventory_stock SET sold_quantity=sold_quantity-?,available_quantity=available_quantity+?,version=version+1 WHERE id=? AND sold_quantity>=?":
                "UPDATE inventory_stock SET sold_quantity=sold_quantity-?,total_quantity=total_quantity-?,version=version+1 WHERE id=? AND sold_quantity>=? AND total_quantity>=?";
        int changed=sellable?jdbc.update(sql,quantity,quantity,stockId,quantity):jdbc.update(sql,quantity,quantity,stockId,quantity,quantity);
        if(changed!=1) throw new BusinessConflictException("退货库存余额不一致");
        jdbc.update("INSERT INTO inventory_return_receipt(command_id,order_id,item_id,stock_id,quantity,disposition) VALUES(?,?,?,?,?,?)",
                commandId,orderId,itemId,stockId,quantity,kind);
        return true;
    }
}
