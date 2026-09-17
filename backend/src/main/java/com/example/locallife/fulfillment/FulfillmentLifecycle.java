package com.example.locallife.fulfillment;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.ForbiddenOperationException;
import com.example.locallife.common.ResourceNotFoundException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class FulfillmentLifecycle {
    private final FulfillmentStore store;
    private final FulfillmentProperties properties;
    private final ObjectMapper json;
    private final com.example.locallife.inventory.InventoryService inventory;

    FulfillmentLifecycle(FulfillmentStore store, FulfillmentProperties properties, ObjectMapper json,
            com.example.locallife.inventory.InventoryService inventory) {
        this.store = store; this.properties = properties; this.json = json;
        this.inventory = inventory;
    }

    @Transactional
    public void enroll(String orderId, String itemType, Long itemId, int quantity) {
        if (!properties.enabled() || !"PRODUCT".equals(itemType)) return;
        String key = "fulfillment-v1:" + orderId;
        try {
            String command = json.writeValueAsString(new WarehouseCommand(key, orderId,
                    itemType, itemId, quantity));
            store.jdbc().update("""
                INSERT INTO fulfillment_task(order_id,request_key,command_json,status)
                VALUES(?,?,?,'WAITING_PAYMENT')
                """, orderId, key, command);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) { throw new IllegalStateException(e); }
    }

    /** Called inside the same transaction and lock order as the order status transition. */
    @Transactional
    public void beforeRefund(String orderId) {
        store.lockOrder(orderId);
        FulfillmentTask task = store.lockTask(orderId);
        if (task == null) return; // Orders created before enrollment retain their existing contract.
        if (!java.util.Set.of("WAITING_PAYMENT", "READY", "CANCELLED").contains(task.status()))
            throw new BusinessConflictException("出库已开始或结果待确认，请先对账处理售后");
        store.jdbc().update("UPDATE fulfillment_task SET status='CANCELLED',updated_at=CURRENT_TIMESTAMP WHERE order_id=?", orderId);
        store.audit(orderId, task.fence(), "REFUND_CANCELLED", "Refund won the dispatch race");
    }

    @Transactional
    public void refunded(String orderId) {
        store.lockOrder(orderId);
        FulfillmentTask task = store.lockTask(orderId);
        if (task == null) return; // Old orders lack proof that physical dispatch never started.
        if (!"CANCELLED".equals(task.status())) throw new BusinessConflictException("履约未取消，不能自动恢复库存");
        if (inventory.restoreUnshipped(orderId))
            store.audit(orderId, task.fence(), "REFUND_RESTOCKED", "Confirmed refund before dispatch");
    }

    @Transactional
    public void enrollCart(String orderId, java.util.List<WarehouseCartCommand.Item> items) {
        requireCartSupport();
        String key="fulfillment-v2:"+orderId+":1";
        String command=cartCommand(orderId,key,1,items);
        store.jdbc().update("INSERT INTO fulfillment_task(order_id,request_key,command_json,status) VALUES(?,?,?,'WAITING_PAYMENT')",orderId,key,command);
        store.jdbc().update("INSERT INTO fulfillment_command_version(order_id,revision,request_key,command_json) VALUES(?,1,?,?)",orderId,key,command);
    }

    public void requireCartSupport() {
        if(!properties.enabled()) throw new BusinessConflictException("购物车需要先启用可靠履约，以保证未出库退款证据");
    }

    @Transactional
    public void holdCartRefund(String orderId) {
        store.lockOrder(orderId);
        var task=store.lockTask(orderId);
        if(task==null || !task.requestKey().startsWith("fulfillment-v2:") || task.fence()!=0
                || !java.util.Set.of("WAITING_PAYMENT","READY").contains(task.status()))
            throw new BusinessConflictException("仅支持从未出库的新购物车订单按数量退款");
        store.jdbc().update("UPDATE fulfillment_task SET status='REFUND_HOLD',next_attempt_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE order_id=?",orderId);
        store.audit(orderId,0,"REFUND_HOLD","Paused before any dispatch claim");
    }

    @Transactional
    public void resumeCartAfterRefund(String orderId, java.util.List<WarehouseCartCommand.Item> remaining) {
        store.lockOrder(orderId);
        var task=store.lockTask(orderId);
        if(task==null || task.fence()!=0 || !"REFUND_HOLD".equals(task.status()))
            throw new BusinessConflictException("退款履约状态不允许自动恢复");
        if(remaining.isEmpty()) {
            store.jdbc().update("UPDATE fulfillment_task SET status='CANCELLED',updated_at=CURRENT_TIMESTAMP WHERE order_id=?",orderId);
            store.audit(orderId,0,"REFUND_ALL","All quantities refunded before dispatch");
            return;
        }
        long revision=store.jdbc().queryForObject("SELECT MAX(revision) FROM fulfillment_command_version WHERE order_id=?",Long.class,orderId)+1;
        String key="fulfillment-v2:"+orderId+":"+revision;
        String command=cartCommand(orderId,key,revision,remaining);
        store.jdbc().update("INSERT INTO fulfillment_command_version(order_id,revision,request_key,command_json) VALUES(?,?,?,?)",orderId,revision,key,command);
        store.jdbc().update("""
            UPDATE fulfillment_task SET request_key=?,command_json=?,status='READY',next_attempt_at=CURRENT_TIMESTAMP,
              updated_at=CURRENT_TIMESTAMP WHERE order_id=?
            """,key,command,orderId);
        store.audit(orderId,0,"REFUND_RESUMED","New immutable command revision="+revision);
    }

    private String cartCommand(String orderId,String key,long revision,java.util.List<WarehouseCartCommand.Item> items) {
        try { return json.writeValueAsString(new WarehouseCartCommand("warehouse.cart.v2",key,orderId,revision,java.util.List.copyOf(items))); }
        catch(com.fasterxml.jackson.core.JsonProcessingException e) { throw new IllegalStateException(e); }
    }

    @Transactional
    public void received(String orderId) {
        store.lockOrder(orderId);
        FulfillmentTask task = store.lockTask(orderId);
        if (task == null) return;
        if (!"SHIPPED".equals(task.status())) throw new BusinessConflictException("订单尚未出库，不能确认收货");
        store.jdbc().update("UPDATE fulfillment_task SET status='RECEIVED',updated_at=CURRENT_TIMESTAMP WHERE order_id=?", orderId);
        store.audit(orderId, task.fence(), "RECEIVED", "Owner confirmed receipt");
    }

    @Transactional(readOnly = true)
    public View get(String orderId, String userId) {
        String owner = store.jdbc().query("SELECT user_id FROM customer_order WHERE id=?",
                (rs, n) -> rs.getString(1), orderId).stream().findFirst()
                .orElseThrow(() -> new ResourceNotFoundException("订单不存在"));
        if (!owner.equals(userId)) throw new ForbiddenOperationException("无权查看该订单履约状态");
        FulfillmentTask task = store.find(orderId);
        if (task == null) throw new ResourceNotFoundException("该订单未启用履约流程");
        return new View(task.orderId(), task.status(), task.trackingNo(), task.attempts(), task.updatedAt());
    }

    @Transactional
    public void retry(String orderId, String actor) {
        String status = store.lockOrder(orderId);
        FulfillmentTask task = store.lockTask(orderId);
        if (!"PAID".equals(status) || task == null || !"NEEDS_REVIEW".equals(task.status()))
            throw new BusinessConflictException("只有待对账的已支付订单可以重试");
        store.audit(orderId, task.fence(), "MANUAL_RETRY", "actor=" + actor + "; previousAttempts=" + task.attempts());
        store.jdbc().update("""
            UPDATE fulfillment_task SET status='UNKNOWN',attempts=0,next_attempt_at=CURRENT_TIMESTAMP,
            owner=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE order_id=?
            """, orderId);
    }

    public record View(String orderId, String status, String trackingNo, int attempts, java.time.LocalDateTime updatedAt) { }
}
