package com.example.locallife.support;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import java.sql.Timestamp;
import java.time.Instant;

/** Restart-safe receipt drain; failures are deferred rather than blocking later receipts. */
@Component
public class SupportReceiptRecovery {
    private final JdbcTemplate jdbc;private final SupportOperations operations;private final boolean enabled;
    private final SupportStockEffects stockEffects;
    private final SupportOrderSimulator orderSimulator;
    private final int maxAttempts;
    public SupportReceiptRecovery(JdbcTemplate jdbc,SupportOperations operations,SupportStockEffects stockEffects,SupportOrderSimulator orderSimulator,
            @Value("${local-life.support.recovery-enabled:false}") boolean enabled,
            @Value("${local-life.support.recovery-max-attempts:8}") int maxAttempts) {
        this.jdbc=jdbc;this.operations=operations;this.stockEffects=stockEffects;this.enabled=enabled;
        this.orderSimulator=orderSimulator;
        if(maxAttempts<1 || maxAttempts>100) throw new IllegalArgumentException("support retry bound must be 1..100");
        this.maxAttempts=maxAttempts;
    }
    @Scheduled(fixedDelayString="${local-life.support.recovery-delay-ms:3000}")
    public void tick() { if(enabled) recover(20); }

    public int recover(int limit) {
        if(limit<1 || limit>100) throw new IllegalArgumentException("recovery batch must be 1..100");
        parkExhausted();
        for(String id:jdbc.query("SELECT id FROM support_order_receipt WHERE status='PENDING' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY attempts,created_at,id LIMIT ?",
                (rs,n)->rs.getString(1),Timestamp.from(Instant.now()),limit)) {
            try {orderSimulator.apply(id);}
            catch(RuntimeException failure) {
                String detail=failure.getClass().getSimpleName()+": "+failure.getMessage();
                jdbc.update("UPDATE support_order_receipt SET attempts=attempts+1,last_error=?,next_attempt_at=? WHERE id=? AND status='PENDING'",
                        detail.substring(0,Math.min(detail.length(),512)),Timestamp.from(Instant.now().plusSeconds(30)),id);
            }
        }
        for(String id:jdbc.query("SELECT effect_id FROM support_stock_effect WHERE status='PENDING' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY attempts,effect_id LIMIT ?",
                (rs,n)->rs.getString(1),Timestamp.from(Instant.now()),limit)) {
            try {
                if(!stockEffects.apply(id)) jdbc.update("UPDATE support_stock_effect SET next_attempt_at=? WHERE effect_id=? AND status='PENDING'",Timestamp.from(Instant.now().plusSeconds(3)),id);
            }
            catch(RuntimeException failure) {
                String detail=failure.getClass().getSimpleName()+": "+failure.getMessage();
                jdbc.update("UPDATE support_stock_effect SET attempts=attempts+1,last_error=?,next_attempt_at=? WHERE effect_id=? AND status='PENDING'",
                        detail.substring(0,Math.min(detail.length(),512)),Timestamp.from(Instant.now().plusSeconds(30)),id);
            }
        }
        for(var item:jdbc.query("SELECT id,user_id FROM support_case WHERE phase='WAITING_STOCK' AND current_type='EXCHANGE' AND recovery_attempts<? AND (recovery_next_at IS NULL OR recovery_next_at<=?) ORDER BY recovery_attempts,updated_at,id LIMIT ?",
                (rs,n)->new Pending(rs.getString(1),rs.getString(2)),maxAttempts,Timestamp.from(Instant.now()),limit)) {
            try {
                operations.reserveReplacement(item.id(),item.owner());
                jdbc.update("UPDATE support_case SET recovery_attempts=recovery_attempts+1,recovery_error=NULL,recovery_next_at=? WHERE id=? AND phase='WAITING_STOCK'",Timestamp.from(Instant.now().plusSeconds(3)),item.id());
            }
            catch(RuntimeException failure) {
                String detail=failure.getClass().getSimpleName()+": "+failure.getMessage();
                jdbc.update("UPDATE support_case SET recovery_attempts=recovery_attempts+1,recovery_error=?,recovery_next_at=? WHERE id=?",
                        detail.substring(0,Math.min(detail.length(),512)),Timestamp.from(Instant.now().plusSeconds(30)),item.id());
            }
        }
        for(var item:jdbc.query("""
            SELECT c.id,c.user_id FROM support_case c JOIN support_replacement r ON r.case_id=c.id
            LEFT JOIN support_scenario_clock t ON t.order_id=c.order_id
            WHERE c.recovery_attempts<? AND (c.recovery_next_at IS NULL OR c.recovery_next_at<=?) AND
             ((c.phase='REPLACEMENT_READY' AND r.status='RESERVED' AND r.reserve_until<=COALESCE(t.virtual_now,?))
               OR (c.phase='REPLACEMENT_RELEASING' AND r.status IN ('RELEASING','RELEASE_REVIEW')))
            ORDER BY c.recovery_attempts,c.updated_at,c.id LIMIT ?
            """,(rs,n)->new Pending(rs.getString(1),rs.getString(2)),maxAttempts,Timestamp.from(Instant.now()),Timestamp.from(Instant.now()),limit)) {
            try {
                operations.expireReplacement(item.id(),item.owner());
                jdbc.update("UPDATE support_case SET recovery_attempts=recovery_attempts+1,recovery_error=NULL,recovery_next_at=? WHERE id=? AND phase IN ('REPLACEMENT_READY','REPLACEMENT_RELEASING')",
                        Timestamp.from(Instant.now().plusSeconds(3)),item.id());
            } catch(RuntimeException failure) {
                String detail=failure.getClass().getSimpleName()+": "+failure.getMessage();
                jdbc.update("UPDATE support_case SET recovery_attempts=recovery_attempts+1,recovery_error=?,recovery_next_at=? WHERE id=?",
                        detail.substring(0,Math.min(detail.length(),512)),Timestamp.from(Instant.now().plusSeconds(30)),item.id());
            }
        }
        var pending=jdbc.query("""
            SELECT r.id,c.user_id FROM support_receipt r JOIN support_case c ON c.id=r.case_id
            WHERE r.status='PENDING' AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=?)
            ORDER BY r.attempts,r.created_at,r.id LIMIT ?
            """,(rs,n)->new Pending(rs.getString(1),rs.getString(2)),Timestamp.from(Instant.now()),limit);
        int applied=0;
        for(var item:pending) {
            try {
                operations.reconcile(item.id(),item.owner());
                if("APPLIED".equals(jdbc.queryForObject("SELECT status FROM support_receipt WHERE id=?",String.class,item.id()))) applied++;
                else jdbc.update("UPDATE support_receipt SET attempts=attempts+1,next_attempt_at=? WHERE id=? AND status='PENDING'",Timestamp.from(Instant.now().plusSeconds(3)),item.id());
            }
            catch(RuntimeException failure) {
                String detail=failure.getClass().getSimpleName()+": "+failure.getMessage();
                jdbc.update("UPDATE support_receipt SET attempts=attempts+1,last_error=?,next_attempt_at=? WHERE id=? AND status='PENDING'",
                        detail.substring(0,Math.min(detail.length(),512)),Timestamp.from(Instant.now().plusSeconds(30)),item.id());
            }
        }
        parkExhausted();
        return applied;
    }
    private void parkExhausted() {
        // Preserve the original payload, key and business phase; no guessed failure or release of holds.
        for(String table:java.util.List.of("support_receipt","support_order_receipt","support_stock_effect"))
            jdbc.update("UPDATE "+table+" SET status='NEEDS_REVIEW',next_attempt_at=NULL WHERE status='PENDING' AND attempts>=?",maxAttempts);
    }
    private record Pending(String id,String owner) { }
}
