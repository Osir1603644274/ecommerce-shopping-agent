package com.example.locallife.payment;

import com.example.locallife.common.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** Local durable provider fixture. This class does not contact a payment provider. */
@Service
public class LocalPartialRefundSimulator {
    private final JdbcTemplate jdbc;
    private final PartialRefundService refunds;
    private final PaymentProperties properties;
    public LocalPartialRefundSimulator(JdbcTemplate jdbc,PartialRefundService refunds,PaymentProperties properties) {
        this.jdbc=jdbc;this.refunds=refunds;this.properties=properties;
    }
    @Transactional
    public void succeed(String id,String user) {
        if(!properties.simulatorEnabled()) throw new ForbiddenOperationException("本地支付模拟器未启用");
        jdbc.query("SELECT id FROM partial_refund WHERE id=? FOR UPDATE",(rs,n)->rs.getString(1),id);
        refunds.get(id,user);
        if(jdbc.queryForObject("SELECT COUNT(*) FROM local_refund_receipt WHERE refund_id=?",Integer.class,id)>0) return;
        jdbc.update("""
            INSERT INTO local_refund_receipt(refund_id,provider_refund_no,payment_id,request_hash,amount_minor,currency)
            SELECT id,?,payment_id,request_hash,amount_minor,currency FROM partial_refund WHERE id=?
            ""","SIM-PARTIAL-"+id,id);
    }
}
