package com.example.locallife.support;

import com.example.locallife.common.*;
import com.example.locallife.ordering.MoneyAllocation;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Timestamp;
import java.time.*;
import java.util.*;

/** Owned preview/confirmation and durable quantity holds. No model-authored facts are accepted. */
@Service
public class SupportService {
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final Clock clock;
    private final boolean enabled;
    private SupportScenarioClock scenarioClock;
    @Autowired(required=false) public void setScenarioClock(SupportScenarioClock value) {this.scenarioClock=value;}
    @Autowired
    public SupportService(JdbcTemplate jdbc, ObjectMapper json,
            @Value("${local-life.support.enabled:false}") boolean enabled) {
        this(jdbc,json,enabled,Clock.systemUTC());
    }
    SupportService(JdbcTemplate jdbc,ObjectMapper json,boolean enabled,Clock clock) {
        this.jdbc=jdbc;this.json=json;this.enabled=enabled;this.clock=clock;
    }
    public record Request(String orderId,Long itemId,Integer quantity,AfterSalePolicy.Type type,String reason) { }
    public record Preview(String previewId,String orderId,long itemId,int quantity,AfterSalePolicy.Type type,
                          long amountMinor,String currency,String policyVersion,Instant expiresAt,String specification) { }
    public record CaseView(String id,String orderId,String userId,long itemId,int quantity,
                           AfterSalePolicy.Type type,AfterSaleLifecycle.Phase phase,long amountMinor,
                           String currency,String reason,String specification,String policyVersion,long version) { }
    public record EventView(String id,String type,String actor,String fromPhase,String toPhase,String evidence,Instant createdAt) { }
    public record ReceiptView(String id,String caseId,String eventType,String status,Instant createdAt,Instant appliedAt) { }
    private record OrderFacts(String status,long version,Instant receivedAt) { }
    private record Quote(long amount,String currency,String specification,String factsHash) { }
    private record StoredPreview(String orderId,String userId,String requestJson,String factsHash,
                                 Instant expiresAt,String caseId) { }

    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Preview preview(Request request,String user) {
        requireEnabled(); validate(request); lockOwned(request.orderId(),user);
        Quote quote=quote(request,user);
        String id=UUID.randomUUID().toString(); Instant expires=businessNow(request.orderId()).plusSeconds(300);
        jdbc.update("UPDATE support_preview SET expires_at=? WHERE order_id=? AND user_id=? AND case_id IS NULL",
                Timestamp.from(businessNow(request.orderId())),request.orderId(),user);
        jdbc.update("INSERT INTO support_preview(id,order_id,user_id,request_json,facts_hash,amount_minor,expires_at) VALUES(?,?,?,?,?,?,?)",
                id,request.orderId(),user,encode(request),quote.factsHash(),quote.amount(),Timestamp.from(expires));
        return new Preview(id,request.orderId(),request.itemId(),request.quantity(),request.type(),quote.amount(),quote.currency(),
                AfterSalePolicy.VERSION,expires,quote.specification());
    }

    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public CaseView confirm(String previewId,String user,String key) {
        requireEnabled(); requireKey(key);
        StoredPreview preview=loadPreview(previewId,user);
        lockOwned(preview.orderId(),user);
        // Re-read after acquiring the same lock used by old quantity refunds.
        preview=loadPreview(previewId,user);
        Request request=decode(preview.requestJson(),Request.class);
        String requestHash=hash(previewId+"\n"+preview.requestJson());
        var replay=jdbc.query("SELECT id,request_hash FROM support_case WHERE order_id=? AND idempotency_key=?",
                (rs,n)->List.of(rs.getString(1),rs.getString(2)),preview.orderId(),key);
        if(!replay.isEmpty()) {
            if(!requestHash.equals(replay.get(0).get(1))) throw conflict("幂等键参数冲突");
            return get(replay.get(0).get(0),user);
        }
        if(preview.caseId()!=null) throw conflict("预览已由另一请求确认，请查询原售后单");
        if(!businessNow(preview.orderId()).isBefore(preview.expiresAt())) throw conflict("确认卡已过期，请重新预览");
        Quote quote=quote(request,user);
        if(!quote.factsHash().equals(preview.factsHash())) throw conflict("订单或售后条件已变化，请重新预览");
        String id=UUID.randomUUID().toString();
        String phase=AfterSaleLifecycle.initial(request.type()).phase().name();
        Timestamp now=Timestamp.from(clock.instant());
        jdbc.update("""
            INSERT INTO support_case(id,order_id,user_id,item_id,quantity,original_type,current_type,phase,
                amount_minor,currency,reason,specification_json,policy_version,idempotency_key,request_hash,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,id,request.orderId(),user,request.itemId(),request.quantity(),request.type().name(),request.type().name(),phase,
                quote.amount(),quote.currency(),request.reason().strip(),quote.specification(),AfterSalePolicy.VERSION,key,requestHash,now,now);
        jdbc.update("INSERT INTO support_order_claim(order_id,case_id,quantity,amount_minor) VALUES(?,?,?,?)",
                request.orderId(),id,request.quantity(),quote.amount());
        jdbc.update("UPDATE support_preview SET case_id=? WHERE id=?",id,previewId);
        jdbc.update("UPDATE customer_order SET version=version+1 WHERE id=?",request.orderId());
        jdbc.update("""
            INSERT INTO support_event(id,case_id,event_key,request_hash,actor,event_type,to_phase,evidence_json,created_at)
            VALUES(?,?,?,?,?,'APPLIED',?,?,?)
            """,UUID.randomUUID().toString(),id,"apply:"+previewId,requestHash,user,phase,encode(Map.of("previewId",previewId)),now);
        return get(id,user);
    }

    @Transactional(readOnly=true)
    public CaseView get(String id,String user) {
        return jdbc.query("SELECT * FROM support_case WHERE id=? AND user_id=?",(rs,n)->new CaseView(
                rs.getString("id"),rs.getString("order_id"),rs.getString("user_id"),rs.getLong("item_id"),rs.getInt("quantity"),
                AfterSalePolicy.Type.valueOf(rs.getString("current_type")),AfterSaleLifecycle.Phase.valueOf(rs.getString("phase")),
                rs.getLong("amount_minor"),rs.getString("currency"),rs.getString("reason"),rs.getString("specification_json"),
                rs.getString("policy_version"),rs.getLong("version")),id,user).stream().findFirst()
                .orElseThrow(()->new ResourceNotFoundException("售后单不存在"));
    }

    @Transactional(readOnly=true)
    public List<CaseView> list(String orderId,String user) {
        requireOwned(orderId,user,false);
        return jdbc.query("SELECT id FROM support_case WHERE order_id=? AND user_id=? ORDER BY created_at DESC,id DESC LIMIT 100",
                (rs,n)->rs.getString(1),orderId,user).stream().map(id->get(id,user)).toList();
    }

    @Transactional(readOnly=true)
    public List<EventView> events(String id,String user) {
        get(id,user);
        return jdbc.query("SELECT * FROM support_event WHERE case_id=? ORDER BY created_at,id",(rs,n)->new EventView(
                rs.getString("id"),rs.getString("event_type"),rs.getString("actor"),rs.getString("from_phase"),
                rs.getString("to_phase"),rs.getString("evidence_json"),rs.getTimestamp("created_at").toInstant()),id);
    }

    @Transactional(readOnly=true)
    public List<ReceiptView> receipts(String id,String user) {
        get(id,user);
        // Public evidence only: never expose actor, payload, retry errors or receipt keys.
        return jdbc.query("SELECT id,case_id,event_type,status,created_at,applied_at FROM support_receipt WHERE case_id=? ORDER BY created_at,id",
                (rs,n)->new ReceiptView(rs.getString("id"),rs.getString("case_id"),rs.getString("event_type"),
                        rs.getString("status"),rs.getTimestamp("created_at").toInstant(),
                        rs.getTimestamp("applied_at")==null?null:rs.getTimestamp("applied_at").toInstant()),id);
    }

    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public CaseView cancel(String id,String user,long expectedVersion,String key) {
        requireEnabled();requireKey(key);
        var first=get(id,user);lockOwned(first.orderId(),user);
        var current=get(id,user);String digest=hash("CANCEL\n"+id+"\n"+expectedVersion);
        var replay=jdbc.query("SELECT request_hash FROM support_event WHERE case_id=? AND event_key=?",(rs,n)->rs.getString(1),id,key);
        if(!replay.isEmpty()) {
            if(!digest.equals(replay.get(0))) throw conflict("幂等键参数冲突");
            return current;
        }
        if(current.version()!=expectedVersion) throw conflict("售后状态已变化，请刷新确认");
        var next=AfterSaleLifecycle.apply(new AfterSaleLifecycle.State(current.type(),current.phase()),AfterSaleLifecycle.Event.CANCEL);
        Timestamp now=Timestamp.from(clock.instant());
        jdbc.update("UPDATE support_case SET phase=?,version=version+1,updated_at=? WHERE id=?",next.phase().name(),now,id);
        jdbc.update("DELETE FROM support_order_claim WHERE case_id=?",id);
        jdbc.update("UPDATE customer_order SET version=version+1 WHERE id=?",current.orderId());
        jdbc.update("""
            INSERT INTO support_event(id,case_id,event_key,request_hash,actor,event_type,from_phase,to_phase,evidence_json,created_at)
            VALUES(?,?,?,?,?,'CANCEL',?,?,?,?)
            """,UUID.randomUUID().toString(),id,key,digest,user,current.phase().name(),next.phase().name(),"{}",now);
        return get(id,user);
    }

    private Quote quote(Request request,String user) {
        OrderFacts order=requireOwned(request.orderId(),user,false);
        var lines=jdbc.query("SELECT quantity,paid_minor,refunded_quantity,refunded_minor FROM order_line_allocation WHERE order_id=? AND item_id=?",
                (rs,n)->new long[]{rs.getInt(1),rs.getLong(2),rs.getInt(3),rs.getLong(4)},request.orderId(),request.itemId());
        if(lines.isEmpty()) throw conflict("此订单明细缺少可靠金额分摊，请创建核实工单");
        long[] line=lines.get(0);
        var payment=jdbc.query("SELECT amount_minor,currency FROM payment_record WHERE order_id=? AND status='SUCCESS'",
                (rs,n)->Map.entry(rs.getLong(1),rs.getString(2)),request.orderId());
        var fulfillment=jdbc.query("SELECT status,fence FROM fulfillment_task WHERE order_id=?",
                (rs,n)->Map.entry(rs.getString(1),rs.getLong(2)),request.orderId());
        String status=fulfillment.isEmpty()?null:fulfillment.get(0).getKey();
        long fence=fulfillment.isEmpty()?0:fulfillment.get(0).getValue();
        boolean active=count("SELECT COUNT(*) FROM support_order_claim WHERE order_id=?",request.orderId())>0
                ||count("SELECT COUNT(*) FROM partial_refund WHERE order_id=? AND status='PROCESSING'",request.orderId())>0;
        if(count("SELECT COUNT(*) FROM refund_record WHERE order_id=?",request.orderId())>0)
            throw conflict("不能与旧整单退款混用，请创建核实工单");
        String spec=specification(request.orderId(),request.itemId());
        String identity="PRODUCT:"+request.itemId();
        int exchanged=jdbc.queryForObject("SELECT COALESCE(SUM(quantity),0) FROM support_case WHERE order_id=? AND item_id=? AND current_type='EXCHANGE' AND phase='COMPLETED'",
                Integer.class,request.orderId(),request.itemId());
        var facts=new AfterSalePolicy.Facts(order.status(),!payment.isEmpty(),status,fence,order.receivedAt(),
                Math.toIntExact(line[0]),Math.toIntExact(line[2]),exchanged,active,identity,spec);
        Instant eligibilityTime=businessNow(request.orderId());
        var decision=AfterSalePolicy.evaluate(facts,new AfterSalePolicy.Request(request.type(),request.quantity(),identity,spec),eligibilityTime);
        if(!decision.eligible() || decision.route()==AfterSalePolicy.Route.UNSHIPPED_REFUND)
            throw conflict(decision.code());
        long amount=MoneyAllocation.refund(line[1],Math.toIntExact(line[0]),Math.addExact(Math.toIntExact(line[2]),exchanged),request.quantity());
        long refunded=jdbc.queryForObject("SELECT COALESCE(SUM(refunded_minor),0) FROM order_line_allocation WHERE order_id=?",Long.class,request.orderId());
        if(amount>payment.get(0).getKey()-refunded) throw conflict("退款金额超过实付余额");
        String digest=hash(encode(List.of(order,request,line[1],line[2],line[3],payment.get(0).getKey(),payment.get(0).getValue(),
                amount,spec==null?"":spec,AfterSalePolicy.VERSION)));
        return new Quote(amount,payment.get(0).getValue(),spec,digest);
    }

    Instant businessNow(String order) {
        // Match TIMESTAMP(6) before writes/comparisons; rounding must not expire
        // superseded cards a fraction of a microsecond into the future.
        Instant now=scenarioClock==null?clock.instant():scenarioClock.now(order,clock.instant());
        return now.truncatedTo(java.time.temporal.ChronoUnit.MICROS);
    }
    private String specification(String order,long item) {
        var values=jdbc.query("SELECT evidence_json FROM order_item WHERE order_id=? AND item_type='PRODUCT' AND item_id=?",
                (rs,n)->rs.getString(1),order,item);
        if(values.size()!=1 || values.get(0)==null) return null;
        try {
            var spec=json.readTree(values.get(0)).path("saleSpecification");
            if(!spec.isObject() || !spec.path("code").isTextual() || spec.path("code").asText().isBlank()) return null;
            return json.writeValueAsString(spec);
        } catch(Exception ex) { return null; }
    }
    private StoredPreview loadPreview(String id,String user) {
        return jdbc.query("SELECT * FROM support_preview WHERE id=? AND user_id=?",(rs,n)->new StoredPreview(
                rs.getString("order_id"),rs.getString("user_id"),rs.getString("request_json"),rs.getString("facts_hash"),
                rs.getTimestamp("expires_at").toInstant(),rs.getString("case_id")),id,user).stream().findFirst()
                .orElseThrow(()->new ResourceNotFoundException("确认卡不存在"));
    }
    private OrderFacts requireOwned(String id,String user,boolean lock) {
        return jdbc.query("SELECT status,version,completed_at FROM customer_order WHERE id=? AND user_id=?"+(lock?" FOR UPDATE":""),
                // Order timestamps are stored as UTC DATETIME by the ordering mapper.
                // Timestamp.toLocalDateTime would reinterpret them in the JVM zone first.
                (rs,n)->new OrderFacts(rs.getString(1),rs.getLong(2),rs.getObject(3,LocalDateTime.class)==null?null:
                        rs.getObject(3,LocalDateTime.class).toInstant(ZoneOffset.UTC)),id,user).stream().findFirst()
                .orElseThrow(()->new ResourceNotFoundException("订单不存在"));
    }
    private void lockOwned(String order,String user) { requireOwned(order,user,true); }
    private int count(String sql,String order) { return jdbc.queryForObject(sql,Integer.class,order); }
    private void requireEnabled() { if(!enabled) throw new ForbiddenOperationException("客服售后写入未启用"); }
    private void validate(Request r) {
        if(r==null || r.orderId()==null || r.orderId().isBlank() || r.itemId()==null || r.itemId()<=0 || r.quantity()==null
                || r.quantity()<=0 || r.type()==null || r.reason()==null || r.reason().isBlank() || r.reason().length()>1000)
            throw new InvalidBusinessStateException("订单、商品、数量、类型和原因必须有效");
    }
    private void requireKey(String key) {
        if(key==null || key.isBlank() || key.length()>128) throw new InvalidBusinessStateException("幂等键必填且最多128字符");
    }
    private String encode(Object value) {
        try { return json.writeValueAsString(value); } catch(Exception ex) { throw new IllegalStateException("序列化售后合同失败",ex); }
    }
    private <T> T decode(String value,Class<T> type) {
        try { return json.readValue(value,type); } catch(Exception ex) { throw new IllegalStateException("售后合同损坏",ex); }
    }
    private static String hash(String value) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8))); }
        catch(Exception ex) { throw new IllegalStateException(ex); }
    }
    private static BusinessConflictException conflict(String message) { return new BusinessConflictException(message); }
}
