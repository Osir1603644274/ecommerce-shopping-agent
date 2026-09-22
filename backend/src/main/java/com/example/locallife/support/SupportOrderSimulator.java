package com.example.locallife.support;

import com.example.locallife.common.*;
import com.example.locallife.fulfillment.*;
import com.example.locallife.ordering.OrderService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;

/** Simulator receipts use the existing fulfillment claim/fence contract; recording and application are separate transactions. */
@Service
public class SupportOrderSimulator {
    public record Receipt(String id,String orderId,String event,String status,String trackingNo) { }
    private final JdbcTemplate jdbc;private final ObjectMapper json;private final FulfillmentEvents events;
    private final FulfillmentClaims claims;private final FulfillmentLifecycle lifecycle;private final OrderService orders;
    private final boolean enabled;private final boolean liveWorker;
    private final SupportScenarioClock clock;
    public SupportOrderSimulator(JdbcTemplate jdbc,ObjectMapper json,FulfillmentEvents events,FulfillmentClaims claims,FulfillmentLifecycle lifecycle,
            OrderService orders,SupportScenarioClock clock,@Value("${local-life.support.simulator-enabled:false}") boolean enabled,
            @Value("${local-life.fulfillment.worker-enabled:false}") boolean liveWorker) {
        this.jdbc=jdbc;this.json=json;this.events=events;this.claims=claims;this.lifecycle=lifecycle;this.orders=orders;this.enabled=enabled;this.liveWorker=liveWorker;
        this.clock=clock;
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Receipt record(String order,boolean received,String tracking,String actor,String key) {
        if(!enabled || liveWorker) throw new ForbiddenOperationException("原订单模拟物流仅限独立模拟器模式且关闭自动出库工作器");
        if(key==null || key.isBlank() || key.length()>128 || tracking==null || !tracking.matches("[A-Za-z0-9_-]{3,128}"))
            throw new InvalidBusinessStateException("物流单号或幂等键无效");
        String owner=lock(order);String event=received?"RECEIVED":"DISPATCH";
        var old=jdbc.queryForList("SELECT * FROM support_order_receipt WHERE order_id=? AND receipt_key=?",order,key);
        if(!old.isEmpty()) {
            var result=view(old.get(0));
            if(!event.equals(result.event()) || !tracking.equals(result.trackingNo())) throw new BusinessConflictException("物流回执幂等键冲突");
            return result;
        }
        if(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_receipt WHERE order_id=? AND event_type=?",Integer.class,order,event)>0)
            throw new BusinessConflictException("该物流事件已有回执，请回查原键");
        FulfillmentTask task;
        if(received) {
            var current=lifecycle.get(order,owner);
            if(!"SHIPPED".equals(current.status()) || !tracking.equals(current.trackingNo())) throw new BusinessConflictException("签收缺少匹配发货单号");
            task=loadTask(order);
        } else {
            events.reconcile(order);
            task=claims.claim(order,"support-simulator").orElseThrow(()->new BusinessConflictException("订单当前不能模拟出库，请检查支付、退款或库存回执"));
        }
        var payload=new WarehouseReceipt(task.requestKey(),order,SupportReceiptSimulator.hash(task.commandJson()),tracking);
        String claim=encode(task),body=encode(payload),id=UUID.randomUUID().toString();
        Instant occurred=clock.now(order,Instant.now()).truncatedTo(java.time.temporal.ChronoUnit.MICROS);
        jdbc.update("INSERT INTO support_order_receipt(id,order_id,event_type,receipt_key,actor,request_hash,claim_json,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                id,order,event,key,actor,SupportReceiptSimulator.hash(event+"\n"+claim+"\n"+body+"\n"+occurred),claim,body,Timestamp.from(occurred));
        return get(id);
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Receipt apply(String id) {
        var initial=get(id);String owner=lock(initial.orderId());
        var row=jdbc.queryForMap("SELECT * FROM support_order_receipt WHERE id=?",id);
        if("APPLIED".equals(row.get("status"))) return view(row);
        String event=(String)row.get("event_type"),claim=(String)row.get("claim_json"),body=(String)row.get("payload_json");
        if(!SupportReceiptSimulator.hash(event+"\n"+claim+"\n"+body+"\n"+((Timestamp)row.get("created_at")).toInstant()).equals(row.get("request_hash"))) throw new BusinessConflictException("物流回执摘要不一致");
        var task=decode(claim,FulfillmentTask.class);var receipt=decode(body,WarehouseReceipt.class);
        if(!initial.orderId().equals(task.orderId()) || !initial.orderId().equals(receipt.orderId())
                ||!task.requestKey().equals(receipt.requestKey()) ||!SupportReceiptSimulator.hash(task.commandJson()).equals(receipt.commandHash()))
            throw new BusinessConflictException("物流回执命令身份不一致");
        if("DISPATCH".equals(event)) {
            if(!claims.shipped(task,receipt)) throw new BusinessConflictException("物流回执对应的出库批次已变化");
        } else if("RECEIVED".equals(event)) {
            var current=lifecycle.get(initial.orderId(),owner);
            if(!"SHIPPED".equals(current.status()) || !receipt.trackingNo().equals(current.trackingNo())) throw new BusinessConflictException("签收与当前物流不匹配");
            orders.complete(initial.orderId(),owner);
            // Receipt time is fixed when the independent simulator records it, including delayed application.
            jdbc.update("UPDATE customer_order SET completed_at=? WHERE id=?",
                    java.time.LocalDateTime.ofInstant(((Timestamp)row.get("created_at")).toInstant(),java.time.ZoneOffset.UTC),initial.orderId());
        } else throw new BusinessConflictException("未知物流事件");
        jdbc.update("UPDATE support_order_receipt SET status='APPLIED',applied_at=? WHERE id=?",Timestamp.from(Instant.now()),id);
        return get(id);
    }
    public Receipt get(String id) {
        var rows=jdbc.queryForList("SELECT * FROM support_order_receipt WHERE id=?",id);
        if(rows.isEmpty()) throw new ResourceNotFoundException("物流回执不存在");return view(rows.get(0));
    }
    private Receipt view(Map<String,Object> row) {return new Receipt((String)row.get("id"),(String)row.get("order_id"),(String)row.get("event_type"),
            (String)row.get("status"),decode((String)row.get("payload_json"),WarehouseReceipt.class).trackingNo());}
    private FulfillmentTask loadTask(String order) {return jdbc.queryForObject("SELECT * FROM fulfillment_task WHERE order_id=?",new org.springframework.jdbc.core.DataClassRowMapper<>(FulfillmentTask.class),order);}
    private String lock(String order) {return jdbc.query("SELECT user_id FROM customer_order WHERE id=? FOR UPDATE",(rs,n)->rs.getString(1),order).stream().findFirst().orElseThrow(()->new ResourceNotFoundException("订单不存在"));}
    private String encode(Object value) {try{return json.writeValueAsString(value);}catch(Exception e){throw new IllegalStateException(e);}}
    private <T>T decode(String value,Class<T> type) {try{return json.readValue(value,type);}catch(Exception e){throw new IllegalStateException(e);}}
}
