package com.example.locallife.support;

import com.example.locallife.integration.EventEnvelope;
import com.example.locallife.integration.InboundEventHandler;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/** Inbox infrastructure persists consumption; this handler never repeats payment or inventory writes. */
@Component
public class SupportAuditEventHandler implements InboundEventHandler {
    public static final String REFUND_COMPLETED="support.refund.completed.v1";
    @Override public boolean supports(String type) { return REFUND_COMPLETED.equals(type); }
    @Override public void handle(EventEnvelope event) {
        LoggerFactory.getLogger(SupportAuditEventHandler.class).info("售后退款事件已消费: eventId={}, orderId={}",event.id(),event.aggregateId());
    }
}
