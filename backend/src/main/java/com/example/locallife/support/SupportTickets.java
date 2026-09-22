package com.example.locallife.support;

import com.example.locallife.common.*;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;

/** Escalation communication only: closing a ticket never settles money or inventory. */
@Service
public class SupportTickets {
    public enum Category { DELIVERY_DELAY, PAYMENT_QUERY, AFTERSALE_DISPUTE, INFO_VERIFY, COMPLAINT }
    public enum Status { OPEN, WAITING_CUSTOMER, RESOLVED, CLOSED }
    public record Request(String orderId,String caseId,Category category,String summary) { }
    public record Ticket(String id,String orderId,String caseId,Category category,Status status,String summary,long version) { }
    public record Event(String id,String actor,String action,String message,Instant createdAt) { }
    private final JdbcTemplate jdbc;private final ObjectMapper json;private final boolean enabled;
    public SupportTickets(JdbcTemplate jdbc,ObjectMapper json,@Value("${local-life.support.enabled:false}") boolean enabled) {
        this.jdbc=jdbc;this.json=json;this.enabled=enabled;
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Ticket create(Request request,String user,String key) {
        enabled();key(key);
        if(request==null || request.category()==null || request.orderId()==null) throw new InvalidBusinessStateException("工单订单和类别必填");
        message(request.summary(),1000);
        if(jdbc.query("SELECT id FROM customer_order WHERE id=? AND user_id=? FOR UPDATE",(rs,n)->rs.getString(1),request.orderId(),user).isEmpty())
            throw new ResourceNotFoundException("订单不存在");
        String hash=hash(request);
        var old=jdbc.queryForList("SELECT id,request_hash FROM support_ticket WHERE order_id=? AND create_key=?",request.orderId(),key);
        if(!old.isEmpty()) {
            if(!hash.equals(old.get(0).get("request_hash"))) throw conflict("工单创建幂等键冲突");
            return get((String)old.get(0).get("id"),user);
        }
        if(request.caseId()!=null && jdbc.queryForObject("SELECT COUNT(*) FROM support_case WHERE id=? AND order_id=? AND user_id=?",Integer.class,
                request.caseId(),request.orderId(),user)!=1) throw new ResourceNotFoundException("关联售后单不存在");
        String id=UUID.randomUUID().toString();Timestamp now=Timestamp.from(Instant.now());
        jdbc.update("INSERT INTO support_ticket(id,order_id,user_id,case_id,category,summary,create_key,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                id,request.orderId(),user,request.caseId(),request.category().name(),request.summary(),key,hash,now,now);
        event(id,"created",hash,user,"CREATED",request.summary());
        return get(id,user);
    }
    public Ticket get(String id,String user) {
        return jdbc.query("SELECT * FROM support_ticket WHERE id=? AND user_id=?",(rs,n)->new Ticket(rs.getString("id"),rs.getString("order_id"),rs.getString("case_id"),
                Category.valueOf(rs.getString("category")),Status.valueOf(rs.getString("status")),rs.getString("summary"),rs.getLong("version")),id,user)
                .stream().findFirst().orElseThrow(()->new ResourceNotFoundException("工单不存在"));
    }
    public List<Ticket> list(String user,int offset,int limit) {
        if(offset<0 || limit<1 || limit>100) throw new InvalidBusinessStateException("分页参数无效");
        return jdbc.query("SELECT id FROM support_ticket WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",(rs,n)->rs.getString(1),user,limit,offset)
                .stream().map(id->get(id,user)).toList();
    }
    public List<Event> events(String id,String user) {
        get(id,user);
        return jdbc.query("SELECT * FROM support_ticket_event WHERE ticket_id=? ORDER BY created_at,id",(rs,n)->new Event(rs.getString("id"),rs.getString("actor"),
                rs.getString("action"),rs.getString("message"),rs.getTimestamp("created_at").toInstant()),id);
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Ticket reply(String id,String user,long version,String message,String key) {
        return change(id,user,user,version,"REPLY",message,key,false);
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Ticket administer(String id,String actor,long version,String action,String message,String key) {
        var owners=jdbc.query("SELECT user_id FROM support_ticket WHERE id=?",(rs,n)->rs.getString(1),id);
        if(owners.isEmpty()) throw new ResourceNotFoundException("工单不存在");
        return change(id,owners.get(0),actor,version,action,message,key,true);
    }
    private Ticket change(String id,String owner,String actor,long version,String action,String message,String key,boolean admin) {
        enabled();key(key);message(message,2000);
        if(action==null || action.isBlank()) throw new InvalidBusinessStateException("工单操作必填");
        jdbc.query("SELECT id FROM support_ticket WHERE id=? AND user_id=? FOR UPDATE",(rs,n)->rs.getString(1),id,owner);
        Ticket current=get(id,owner);String hash=hash(List.of(actor,version,action,message,admin));
        var old=jdbc.query("SELECT request_hash FROM support_ticket_event WHERE ticket_id=? AND event_key=?",(rs,n)->rs.getString(1),id,key);
        if(!old.isEmpty()) {if(!old.get(0).equals(hash)) throw conflict("工单操作幂等键冲突");return current;}
        if(current.version()!=version) throw conflict("工单版本已变化");
        Status next=null;
        if(!admin && "REPLY".equals(action) && current.status()!=Status.CLOSED) next=Status.OPEN;
        if(admin) next=switch(action) {
            case "REQUEST_INFO" -> current.status()==Status.OPEN?Status.WAITING_CUSTOMER:null;
            case "RESOLVE" -> Set.of(Status.OPEN,Status.WAITING_CUSTOMER).contains(current.status())?Status.RESOLVED:null;
            case "CLOSE" -> current.status()==Status.RESOLVED?Status.CLOSED:null;
            default -> null;
        };
        if(next==null) throw conflict("工单状态不允许此操作");
        jdbc.update("UPDATE support_ticket SET status=?,version=version+1,updated_at=? WHERE id=?",next.name(),Timestamp.from(Instant.now()),id);
        event(id,key,hash,actor,action,message);return get(id,owner);
    }
    private void event(String id,String key,String hash,String actor,String action,String message) {
        jdbc.update("INSERT INTO support_ticket_event(id,ticket_id,event_key,request_hash,actor,action,message,created_at) VALUES(?,?,?,?,?,?,?,?)",
                UUID.randomUUID().toString(),id,key,hash,actor,action,message,Timestamp.from(Instant.now()));
    }
    private String hash(Object body) {try{return SupportReceiptSimulator.hash(json.writeValueAsString(body));}catch(Exception e){throw new IllegalStateException(e);}}
    private void key(String value) {if(value==null || value.isBlank() || value.length()>128) throw new InvalidBusinessStateException("工单幂等键无效");}
    private void message(String value,int max) {if(value==null || value.isBlank() || value.length()>max) throw new InvalidBusinessStateException("工单说明长度无效");}
    private void enabled() {if(!enabled) throw new ForbiddenOperationException("客服工单写入未启用");}
    private BusinessConflictException conflict(String message) {return new BusinessConflictException(message);}
}
