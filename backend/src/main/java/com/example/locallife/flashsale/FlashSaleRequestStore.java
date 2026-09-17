package com.example.locallife.flashsale;

import com.example.locallife.common.BusinessConflictException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;

@Service
class FlashSaleRequestStore {
    private final JdbcTemplate jdbc;
    @org.springframework.beans.factory.annotation.Value("${local-life.flash-sale.sql-recovery-age-seconds:300}")
    private long recoveryAgeSeconds = 300;

    FlashSaleRequestStore(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    String acceptedFor(long campaign, String user) {
        var existing = jdbc.query("SELECT id,status FROM flash_sale_request WHERE campaign_id=? AND user_id=?",
                (rs,row) -> new String[]{rs.getString(1),rs.getString(2)},campaign,user);
        if (existing.isEmpty()) return null;
        if ("DEAD".equals(existing.get(0)[1]) || "COMPENSATING".equals(existing.get(0)[1]))
            throw new BusinessConflictException("此前秒杀请求已进入死信，请先核对处理结果");
        return existing.get(0)[0];
    }

    // The HTTP caller returns QUEUED only after this independent transaction commits.
    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public String accept(String id, long campaign, String user, long amount) {
        List<String> existing = jdbc.query(
                "SELECT id FROM flash_sale_request WHERE campaign_id=? AND user_id=?",
                (rs, row) -> rs.getString(1), campaign, user);
        if (!existing.isEmpty()) return existing.get(0);
        jdbc.update("INSERT INTO flash_sale_request(id,campaign_id,user_id,amount_minor) VALUES(?,?,?,?)",
                id, campaign, user, amount);
        return id;
    }

    void assertNotDead(String id) {
        List<String> statuses = jdbc.query("SELECT status FROM flash_sale_request WHERE id=? FOR UPDATE",
                (rs, row) -> rs.getString(1), id);
        if (statuses.contains("DEAD") || statuses.contains("COMPENSATING"))
            throw new BusinessConflictException("秒杀请求已经进入死信，禁止迟到落单");
    }

    void complete(String id, String orderId) {
        jdbc.update("UPDATE flash_sale_request SET status='COMPLETED',completed_order_id=?,last_error=NULL WHERE id=?",
                orderId, id);
    }

    void dead(String id, String error) {
        jdbc.update("UPDATE flash_sale_request SET status='COMPENSATING',last_error=? WHERE id=? AND status='PENDING'",
                error, id);
    }

    void compensated(String id) {
        jdbc.update("UPDATE flash_sale_request SET status='DEAD' WHERE id=? AND status='COMPENSATING'",id);
    }

    List<Request> pending() {
        LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);
        return jdbc.query("""
                SELECT id,campaign_id,user_id,amount_minor,attempts,status FROM flash_sale_request
                WHERE status IN ('PENDING','COMPENSATING') AND next_attempt_at<=? AND (status='COMPENSATING' OR created_at<=?)
                ORDER BY next_attempt_at,created_at LIMIT 100
                """, (rs, row) -> new Request(rs.getString(1), rs.getLong(2), rs.getString(3),
                rs.getLong(4), rs.getInt(5), rs.getString(6)), now, now.minusSeconds(recoveryAgeSeconds));
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public void retry(String id, String error) {
        jdbc.update("""
                UPDATE flash_sale_request SET attempts=attempts+1,next_attempt_at=?,last_error=?
                WHERE id=? AND status IN ('PENDING','COMPENSATING')
                """, LocalDateTime.now(ZoneOffset.UTC).plusSeconds(30), error.substring(0, Math.min(1000,error.length())), id);
    }

    record Request(String id, long campaignId, String userId, long amountMinor, int attempts, String status) { }
}
