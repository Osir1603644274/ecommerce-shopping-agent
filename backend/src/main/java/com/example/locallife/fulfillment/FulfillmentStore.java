package com.example.locallife.fulfillment;

import com.example.locallife.common.ResourceNotFoundException;
import org.springframework.jdbc.core.DataClassRowMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
class FulfillmentStore {
    private final JdbcTemplate jdbc;
    private final DataClassRowMapper<FulfillmentTask> rowMapper = new DataClassRowMapper<>(FulfillmentTask.class);

    FulfillmentStore(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    JdbcTemplate jdbc() { return jdbc; }

    FulfillmentTask find(String orderId) {
        return jdbc.query("SELECT * FROM fulfillment_task WHERE order_id = ?", rowMapper, orderId)
                .stream().findFirst().orElse(null);
    }

    FulfillmentTask lockTask(String orderId) {
        return jdbc.query("SELECT * FROM fulfillment_task WHERE order_id = ? FOR UPDATE", rowMapper, orderId)
                .stream().findFirst().orElse(null);
    }

    String lockOrder(String orderId) {
        return jdbc.query("SELECT status FROM customer_order WHERE id = ? FOR UPDATE",
                (rs, row) -> rs.getString(1), orderId).stream().findFirst()
                .orElseThrow(() -> new ResourceNotFoundException("订单不存在"));
    }

    void audit(String order, long fence, String outcome, String detail) {
        jdbc.update("INSERT INTO fulfillment_attempt(order_id,fence,outcome,detail) VALUES(?,?,?,?)",
                order, fence, outcome, detail == null ? null : detail.substring(0, Math.min(1000, detail.length())));
    }
}
