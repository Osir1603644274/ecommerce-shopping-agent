package com.example.locallife.support;

import com.example.locallife.common.*;
import com.example.locallife.inventory.ReturnedInventory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** Consumes only server-created, inspection-bound effects, retaining identity across retries. */
@Service
public class SupportStockEffects {
    private final JdbcTemplate jdbc;private final ReturnedInventory inventory;
    public SupportStockEffects(JdbcTemplate jdbc,ReturnedInventory inventory) { this.jdbc=jdbc;this.inventory=inventory; }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public boolean apply(String effectId) {
        var ids=jdbc.query("SELECT order_id FROM support_stock_effect WHERE effect_id=?",(rs,n)->rs.getString(1),effectId);
        if(ids.isEmpty()) throw new ResourceNotFoundException("库存待办不存在");
        jdbc.query("SELECT id FROM customer_order WHERE id=? FOR UPDATE",(rs,n)->rs.getString(1),ids.get(0));
        var effect=jdbc.queryForMap("SELECT * FROM support_stock_effect WHERE effect_id=?",effectId);
        if("ACK".equals(effect.get("status"))) return true;
        String kind=(String)effect.get("kind"),caseId=(String)effect.get("case_id");
        if(!java.util.Set.of("RETURN_SELLABLE","RETURN_QUARANTINE").contains(kind))
            throw new BusinessConflictException("不支持的库存待办类型");
        int quantity=((Number)effect.get("quantity")).intValue();long item=((Number)effect.get("item_id")).longValue();
        int evidence=jdbc.queryForObject("""
            SELECT COUNT(*) FROM support_return r JOIN support_receipt p ON p.id=r.inspection_receipt_id
            WHERE r.case_id=? AND r.item_id=? AND r.received_quantity=? AND r.sellable=?
              AND p.status='APPLIED' AND p.event_type='INSPECTION_ACCEPTED'
            """,Integer.class,caseId,item,quantity,"RETURN_SELLABLE".equals(kind));
        if(evidence!=1) throw new BusinessConflictException("库存待办缺少匹配验收回执");
        boolean applied=inventory.receive(effectId,ids.get(0),item,quantity,"RETURN_SELLABLE".equals(kind));
        jdbc.update("UPDATE support_stock_effect SET status=?,receipt_id=?,attempts=attempts+1,last_error=NULL WHERE effect_id=?",
                applied?"ACK":"PENDING",applied?effectId:null,effectId);
        return applied;
    }
}
