package com.example.locallife.support;

import com.example.locallife.common.*;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;
import static com.example.locallife.support.AfterSaleLifecycle.Event;

/** Independent durable simulator receipt transaction. Does not settle money or alter stock. */
@Service
public class SupportReceiptSimulator {
    private final JdbcTemplate jdbc;private final ObjectMapper json;private final boolean enabled;private final SupportScenarioClock clock;
    public SupportReceiptSimulator(JdbcTemplate jdbc,ObjectMapper json,SupportScenarioClock clock,
            @Value("${local-life.support.simulator-enabled:false}") boolean enabled) {
        this.jdbc=jdbc;this.json=json;this.enabled=enabled;this.clock=clock;
    }
    public record Input(Event event,long expectedVersion,Long itemId,Integer quantity,Boolean sellable,String reason) { }
    public record Receipt(String id,String caseId,String event,String status,String payload,long expectedVersion) { }

    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Receipt record(String id,Input input,String actor,String key) {
        requireEnabled();
        if(input==null || input.event()==null || !Set.of(Event.APPROVE,Event.REJECT,Event.RETURN_RECEIVED,
                Event.INSPECTION_ACCEPTED,Event.INSPECTION_DISPUTED).contains(input.event()) || input.expectedVersion()<0
                || input.reason()==null || input.reason().isBlank() || input.reason().length()>1000)
            throw new InvalidBusinessStateException("模拟回执类型、版本和说明必须有效");
        lock(id);
        return insert(id,input.event(),input.expectedVersion(),encode(input),actor,key);
    }

    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Receipt refundSucceeded(String id,String actor,String key) {
        requireEnabled();lock(id);
        var commands=jdbc.queryForList("SELECT payment_id,request_hash,amount_minor,currency FROM support_refund_command WHERE case_id=?",id);
        if(commands.isEmpty()) throw new BusinessConflictException("尚无待执行的退款命令");
        // Payload is copied from the durable command, never from customer/model arguments.
        var command=commands.get(0);
        var payload=new TreeMap<String,Object>();
        payload.put("paymentId",command.get("payment_id"));payload.put("commandHash",command.get("request_hash"));
        payload.put("amountMinor",command.get("amount_minor"));payload.put("currency",command.get("currency"));
        payload.put("providerReference","SIM-SUPPORT-"+id);
        // Use command-stable version: replay after successful reconciliation must stay identical.
        long version=jdbc.queryForObject("SELECT version FROM support_case WHERE id=?",Long.class,id);
        var existing=jdbc.query("SELECT id FROM support_receipt WHERE case_id=? AND receipt_key=?",(rs,n)->rs.getString(1),id,key);
        if(!existing.isEmpty()) {
            Receipt result=get(existing.get(0));
            if(!result.event().equals(Event.REFUND_RECEIPT_CONFIRMED.name()) || !result.payload().equals(encode(payload)))
                throw new BusinessConflictException("模拟回执幂等键冲突");
            return result;
        }
        return insert(id,Event.REFUND_RECEIPT_CONFIRMED,version,encode(payload),actor,key);
    }

    public Receipt get(String receiptId) {
        return jdbc.query("SELECT * FROM support_receipt WHERE id=?",(rs,n)->new Receipt(rs.getString("id"),rs.getString("case_id"),
                rs.getString("event_type"),rs.getString("status"),rs.getString("payload_json"),rs.getLong("expected_version")),receiptId)
                .stream().findFirst().orElseThrow(()->new ResourceNotFoundException("回执不存在"));
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Receipt replacementEvent(String id,boolean received,String tracking,String actor,String key) {
        requireEnabled();lock(id);
        if(tracking==null || !tracking.matches("[A-Za-z0-9_-]{3,128}")) throw new InvalidBusinessStateException("补发单号格式无效");
        Event event=received?Event.REPLACEMENT_RECEIPT_CONFIRMED:Event.REPLACEMENT_DISPATCH_CONFIRMED;
        var rows=jdbc.queryForList("SELECT * FROM support_replacement WHERE case_id=? AND status IN ('RESERVED','SHIPPED','RECEIVED')",id);
        if(rows.size()!=1) throw new BusinessConflictException("缺少唯一已预占的补发记录");
        var row=rows.get(0);var payload=new TreeMap<String,Object>();
        payload.put("replacementId",row.get("id"));payload.put("itemId",row.get("item_id"));payload.put("quantity",row.get("quantity"));
        payload.put("specification",row.get("specification"));payload.put("trackingNo",tracking);payload.put("chargeMinor",0);
        String encoded=encode(payload);
        var prior=jdbc.query("SELECT id FROM support_receipt WHERE case_id=? AND receipt_key=?",(rs,n)->rs.getString(1),id,key);
        if(!prior.isEmpty()) {
            var old=get(prior.get(0));
            if(!old.event().equals(event.name()) || !old.payload().equals(encoded)) throw new BusinessConflictException("补发回执幂等键冲突");
            return old;
        }
        if(!(received?"SHIPPED":"RESERVED").equals(row.get("status"))) throw new BusinessConflictException("补发状态不允许生成该物流回执");
        if(!received) {
            String order=jdbc.queryForObject("SELECT order_id FROM support_case WHERE id=?",String.class,id);
            Timestamp deadline=(Timestamp)row.get("reserve_until");
            if(deadline==null || !deadline.toInstant().isAfter(clock.now(order,Instant.now())))
                throw new BusinessConflictException("预占期限已到，请先核实释放");
        }
        if(received && !tracking.equals(row.get("tracking_no"))) throw new BusinessConflictException("补发签收单号不匹配");
        long version=jdbc.queryForObject("SELECT version FROM support_case WHERE id=?",Long.class,id);
        return insert(id,event,version,encoded,actor,key);
    }
    private Receipt insert(String id,Event event,long version,String payload,String actor,String key) {
        if(key==null || key.isBlank() || key.length()>128) throw new InvalidBusinessStateException("回执幂等键必填且最多128字符");
        String digest=hash(event+"\n"+version+"\n"+payload);
        var existing=jdbc.query("SELECT id,request_hash FROM support_receipt WHERE case_id=? AND receipt_key=?",
                (rs,n)->List.of(rs.getString(1),rs.getString(2)),id,key);
        if(!existing.isEmpty()) {
            if(!existing.get(0).get(1).equals(digest)) throw new BusinessConflictException("模拟回执幂等键冲突");
            return get(existing.get(0).get(0));
        }
        // The shared order lock serializes distinct keys for one simulator action.
        boolean terminalEffect=Set.of(Event.REFUND_RECEIPT_CONFIRMED,Event.REPLACEMENT_DISPATCH_CONFIRMED,
                Event.REPLACEMENT_RECEIPT_CONFIRMED).contains(event);
        int occupied=terminalEffect?
                jdbc.queryForObject("SELECT COUNT(*) FROM support_receipt WHERE case_id=? AND event_type=?",Integer.class,id,event.name()):
                jdbc.queryForObject("SELECT COUNT(*) FROM support_receipt WHERE case_id=? AND expected_version=?",Integer.class,id,version);
        if(occupied>0) throw new BusinessConflictException("此办理阶段已有独立回执，请使用原键回查");
        String receipt=UUID.randomUUID().toString();
        jdbc.update("""
            INSERT INTO support_receipt(id,case_id,receipt_key,request_hash,event_type,expected_version,actor,payload_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,receipt,id,key,digest,event.name(),version,actor,payload,Timestamp.from(Instant.now()));
        return get(receipt);
    }
    private void lock(String id) {
        var orders=jdbc.query("SELECT order_id FROM support_case WHERE id=?",(rs,n)->rs.getString(1),id);
        if(orders.isEmpty()) throw new ResourceNotFoundException("售后单不存在");
        jdbc.query("SELECT id FROM customer_order WHERE id=? FOR UPDATE",(rs,n)->rs.getString(1),orders.get(0));
    }
    private void requireEnabled() { if(!enabled) throw new ForbiddenOperationException("客服模拟器未启用"); }
    private String encode(Object value) { try{return json.writeValueAsString(value);}catch(Exception ex){throw new IllegalStateException(ex);} }
    static String hash(String value) {
        try{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));}
        catch(Exception ex){throw new IllegalStateException(ex);}
    }
}
