package com.example.locallife.support;

import com.example.locallife.common.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.sql.Timestamp;
import java.time.Instant;

/** Explicit per-order simulator time; never changes the host clock or unrelated orders. */
@Service
public class SupportScenarioClock {
    public record View(String orderId,Instant now,long version) { }
    private final JdbcTemplate jdbc;private final boolean enabled;
    public SupportScenarioClock(JdbcTemplate jdbc,@Value("${local-life.support.simulator-enabled:false}") boolean enabled) {this.jdbc=jdbc;this.enabled=enabled;}
    public Instant now(String order,Instant fallback) {
        return jdbc.query("SELECT virtual_now FROM support_scenario_clock WHERE order_id=?",(rs,n)->rs.getTimestamp(1).toInstant(),order).stream().findFirst().orElse(fallback);
    }
    public View get(String order) {
        return jdbc.query("SELECT virtual_now,version FROM support_scenario_clock WHERE order_id=?",(rs,n)->new View(order,rs.getTimestamp(1).toInstant(),rs.getLong(2)),order)
                .stream().findFirst().orElseThrow(()->new ResourceNotFoundException("订单未绑定模拟时钟"));
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public View bind(String order,String actor) {
        lock(order);
        if(jdbc.queryForObject("SELECT COUNT(*) FROM support_scenario_clock WHERE order_id=?",Integer.class,order)>0) return get(order);
        if(jdbc.queryForObject("SELECT COUNT(*) FROM support_order_receipt WHERE order_id=?",Integer.class,order)>0
                ||jdbc.queryForObject("SELECT COUNT(*) FROM customer_order WHERE id=? AND completed_at IS NOT NULL",Integer.class,order)>0)
            throw new BusinessConflictException("不能为已有物流回执或签收历史的订单重设时钟");
        jdbc.update("INSERT INTO support_scenario_clock(order_id,virtual_now,actor) VALUES(?,?,?)",order,Timestamp.from(Instant.now()),actor);
        return get(order);
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public View advance(String order,long version,long seconds,String actor,String key) {
        lock(order);
        if(seconds<=0 || seconds>31536000 || key==null || key.isBlank() || key.length()>128) throw new InvalidBusinessStateException("时钟推进必须为正秒数且最多一年，幂等键必填");
        String hash=SupportReceiptSimulator.hash(version+"\n"+seconds);
        var old=jdbc.query("SELECT request_hash FROM support_clock_event WHERE order_id=? AND event_key=?",(rs,n)->rs.getString(1),order,key);
        if(!old.isEmpty()) {if(!hash.equals(old.get(0))) throw new BusinessConflictException("时钟幂等键冲突");return get(order);}
        View current=get(order);
        if(current.version()!=version) throw new BusinessConflictException("时钟版本已变化");
        Instant next=current.now().plusSeconds(seconds);
        jdbc.update("UPDATE support_scenario_clock SET virtual_now=?,version=version+1 WHERE order_id=?",Timestamp.from(next),order);
        jdbc.update("INSERT INTO support_clock_event(order_id,event_key,request_hash,actor,from_time,to_time) VALUES(?,?,?,?,?,?)",
                order,key,hash,actor,Timestamp.from(current.now()),Timestamp.from(next));
        return get(order);
    }
    private void lock(String order) {
        if(!enabled) throw new ForbiddenOperationException("模拟时钟未启用");
        if(jdbc.query("SELECT id FROM customer_order WHERE id=? FOR UPDATE",(rs,n)->rs.getString(1),order).isEmpty()) throw new ResourceNotFoundException("订单不存在");
    }
}
