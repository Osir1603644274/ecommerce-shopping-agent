package com.example.locallife.integration;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.fulfillment.FulfillmentEvents;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.ordering.*;
import com.example.locallife.payment.*;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import java.time.LocalDateTime;
import java.util.*;
import java.util.concurrent.atomic.AtomicLong;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@SpringBootTest(properties={"local-life.fulfillment.enabled=true","local-life.messaging.max-attempts=1",
        "spring.datasource.url=jdbc:h2:mem:cart_refund_events_v4;MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1"})
class CartRefundEventIntegrationTests {
    @Autowired CartOrderService carts;
    @Autowired InventoryService inventory;
    @Autowired PaymentService payments;
    @Autowired PartialRefundService refunds;
    @Autowired LocalPartialRefundSimulator provider;
    @Autowired FulfillmentEvents fulfillment;
    @Autowired InboundEventProcessor processor;
    @Autowired OutboxMapper outbox;
    @Autowired JdbcTemplate jdbc;
    @MockitoBean CommerceCatalogPort catalog;
    static final AtomicLong ids=new AtomicLong(810000);
    String user,order;long product;

    @BeforeEach void prepare() {
        user=UUID.randomUUID().toString();product=ids.incrementAndGet();
        jdbc.update("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES(?,?,'test',TRUE,0)",user,user);
        inventory.createStock("PRODUCT",product,10);
        when(catalog.requireItem(eq("PRODUCT"),eq(product))).thenReturn(new CommerceItemSnapshot("PRODUCT",product,"fixture",101,"CNY",1,"{}"));
        order=carts.create(new CreateCartOrderRequest(List.of(new CreateCartOrderRequest.Line("PRODUCT",product,3)),null),user,"cart").id();
        var payment=payments.create(order,user);payments.simulateSuccess(payment.id(),user);
    }

    @Test void realPartialRefundOutboxIsProcessedByBothConsumersAndDuplicatePayloadConflictIsRejected() {
        var event=settleFirstRefund();
        String command=jdbc.queryForObject("SELECT command_json FROM fulfillment_task WHERE order_id=?",String.class,order);
        for(int i=0;i<2;i++) {processor.process("domain-event-projection",event);fulfillment.accept(event);}
        assertBothProcessed(event);
        assertThat(jdbc.queryForObject("SELECT command_json FROM fulfillment_task WHERE order_id=?",String.class,order)).isEqualTo(command);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_command_version WHERE order_id=?",Integer.class,order)).isEqualTo(2);
        assertThat(inventory.getStock("PRODUCT",product).soldQuantity()).isEqualTo(2);
        var conflict=new EventEnvelope(event.id(),event.aggregateType(),event.aggregateId(),event.eventType(),"{\"tampered\":true}",event.occurredAt());
        assertThatThrownBy(()->processor.process("domain-event-projection",conflict)).isInstanceOf(BusinessConflictException.class);
        assertThatThrownBy(()->fulfillment.accept(conflict)).isInstanceOf(BusinessConflictException.class);
        assertBothProcessed(event);
        System.out.println("V4_EVIDENCE partial_refund_event consumers=2 processed_receipts=2 duplicate_deliveries=2 own_dead_letters=0");
    }

    @Test void delayedPriorPartialRefundEventCannotReleaseNewRefundHold() {
        var event=settleFirstRefund();
        refunds.create(order,user,"second",new PartialRefundRequest(List.of(new PartialRefundRequest.Line(product,1)),"next quantity"));
        processor.process("domain-event-projection",event);fulfillment.accept(event);fulfillment.accept(event);
        assertBothProcessed(event);
        assertThat(jdbc.queryForObject("SELECT status FROM fulfillment_task WHERE order_id=?",String.class,order)).isEqualTo("REFUND_HOLD");
        assertThat(inventory.getStock("PRODUCT",product).soldQuantity()).isEqualTo(2);
    }

    private EventEnvelope settleFirstRefund() {
        var refund=refunds.create(order,user,"first",new PartialRefundRequest(List.of(new PartialRefundRequest.Line(product,1)),"first quantity"));
        provider.succeed(refund.id(),user);refunds.reconcile(refund.id(),user);
        String id=jdbc.queryForObject("SELECT id FROM outbox_event WHERE aggregate_id=? AND event_type=?",String.class,order,DomainEventTypes.ORDER_PARTIAL_REFUNDED_V2);
        var persisted=outbox.findById(id);
        return new EventEnvelope(persisted.id(),persisted.aggregateType(),persisted.aggregateId(),persisted.eventType(),persisted.payloadJson(),persisted.occurredAt());
    }
    private void assertBothProcessed(EventEnvelope event) {
        assertThat(jdbc.queryForList("SELECT consumer_name FROM inbox_event WHERE event_id=? AND status='PROCESSED' ORDER BY consumer_name",String.class,event.id()))
                .containsExactly("domain-event-projection","fulfillment-v1");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM dead_letter_event WHERE event_id=?",Integer.class,event.id())).isZero();
    }
}
