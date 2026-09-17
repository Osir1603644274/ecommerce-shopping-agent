package com.example.locallife.fulfillment;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.integration.EventEnvelope;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.ordering.*;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import java.time.LocalDateTime;
import java.util.UUID;

import static org.assertj.core.api.Assertions.*;

@SpringBootTest(properties = {"local-life.fulfillment.enabled=true", "local-life.fulfillment.max-attempts=2"})
@Transactional
class FulfillmentIntegrationTests {
    @Autowired OrderService orders;
    @Autowired InventoryService inventory;
    @Autowired FulfillmentLifecycle lifecycle;
    @Autowired FulfillmentEvents events;
    @Autowired FulfillmentClaims claims;
    @Autowired FulfillmentStore store;
    @Autowired JdbcTemplate jdbc;
    @Autowired ObjectMapper json;
    @Autowired FulfillmentDeadLetters deadLetters;
    @Autowired com.example.locallife.payment.PaymentService payments;
    String user;

    @BeforeEach void prepare() {
        user = UUID.randomUUID().toString();
        jdbc.update("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES(?,?,'x',TRUE,0)", user, user);
        inventory.createStock("PRODUCT", 1001L, 10);
    }

    private String order() {
        return orders.create(new CreateOrderRequest("PRODUCT", 1001L, 1, null), user, UUID.randomUUID().toString()).id();
    }
    private EventEnvelope event(String order) {
        return new EventEnvelope(UUID.randomUUID().toString(), "ORDER", order, "order.paid.v1", "{}", LocalDateTime.now());
    }
    private FulfillmentTask paid() {
        String order = order(); orders.markPaid(order); events.accept(event(order)); return store.find(order);
    }
    private WarehouseReceipt receipt(FulfillmentTask task) {
        return new WarehouseReceipt(task.requestKey(), task.orderId(), FulfillmentEvents.hash(task.commandJson()), "SIM-TEST");
    }

    @Test void duplicateDeliveryAndDistinctEventsCreateOneTaskAndOneReadyTransition() {
        String id = order(); orders.markPaid(id);
        EventEnvelope event = event(id); events.accept(event); events.accept(event); events.accept(event(id));
        assertThat(store.find(id).status()).isEqualTo("READY");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_task WHERE order_id=?", Integer.class, id)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_attempt WHERE order_id=? AND outcome='READY'", Integer.class, id)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT status FROM inbox_event WHERE consumer_name='fulfillment-v1' AND event_id=?", String.class, event.id())).isEqualTo("PROCESSED");
    }

    @Test void conflictingEventIdIsRejectedEvenWithDifferentAggregate() {
        FulfillmentTask first = paid(); EventEnvelope event = event(first.orderId()); events.accept(event);
        assertThatThrownBy(() -> events.accept(new EventEnvelope(event.id(), "ORDER", first.orderId(),
                "order.paid.v1", "{\"changed\":true}", event.occurredAt())))
                .isInstanceOf(BusinessConflictException.class);
        String other = order();
        assertThatThrownBy(() -> events.accept(new EventEnvelope(event.id(), "ORDER", other,
                event.eventType(), event.payloadJson(), event.occurredAt())))
                .isInstanceOf(BusinessConflictException.class);
        assertThat(store.find(other).status()).isEqualTo("WAITING_PAYMENT");
    }

    @Test void refundWinsBeforeDispatchAndOldPaymentEventCannotReviveTask() {
        FulfillmentTask task = paid(); orders.markRefunding(task.orderId()); events.accept(event(task.orderId()));
        assertThat(store.find(task.orderId()).status()).isEqualTo("CANCELLED");
        assertThat(claims.claim(task.orderId(), "worker")).isEmpty();
    }

    @Test void dispatchLeaseBlocksRefundAndExpiredWorkerCannotOverwriteNewClaim() {
        FulfillmentTask task = paid();
        FulfillmentTask first = claims.claim(task.orderId(), "worker-old").orElseThrow();
        assertThat(claims.claim(task.orderId(), "worker-new")).isEmpty();
        assertThatThrownBy(() -> orders.markRefunding(task.orderId())).isInstanceOf(BusinessConflictException.class);
        jdbc.update("UPDATE fulfillment_task SET lease_until=DATEADD('SECOND',-60,CURRENT_TIMESTAMP) WHERE order_id=?", task.orderId());
        FulfillmentTask second = claims.claim(task.orderId(), "worker-new").orElseThrow();
        assertThat(second.fence()).isGreaterThan(first.fence());
        assertThat(claims.shipped(first, receipt(first))).isFalse();
        assertThat(claims.failed(first, "late failure")).isFalse();
        assertThat(claims.shipped(second, receipt(second))).isTrue();
        assertThat(store.find(task.orderId()).status()).isEqualTo("SHIPPED");
    }

    @Test void successfulPreDispatchRefundRestoresStockOnceAcrossRepeatedCallbacks() {
        String id = order();
        var payment = payments.create(id, user);
        payments.simulateSuccess(payment.id(), user);
        events.accept(event(id));
        var refund = payments.requestRefund(id, user, "cancel before dispatch");
        payments.simulateRefundSuccess(refund.id(), user);
        payments.simulateRefundSuccess(refund.id(), user);
        assertThat(inventory.getStock("PRODUCT", 1001L).availableQuantity()).isEqualTo(10);
        assertThat(inventory.getStock("PRODUCT", 1001L).soldQuantity()).isZero();
        assertThat(store.find(id).status()).isEqualTo("CANCELLED");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_attempt WHERE order_id=? AND outcome='REFUND_RESTOCKED'", Integer.class, id)).isEqualTo(1);
    }

    @Test void unknownResultKeepsIdentityAndRequiresExplicitRetryAfterBudget() {
        FulfillmentTask task = paid();
        FulfillmentTask first = claims.claim(task.orderId(), "one").orElseThrow(); claims.failed(first, "response lost");
        assertThat(store.find(task.orderId()).status()).isEqualTo("UNKNOWN");
        jdbc.update("UPDATE fulfillment_task SET next_attempt_at=DATEADD('SECOND',-1,CURRENT_TIMESTAMP) WHERE order_id=?", task.orderId());
        FulfillmentTask second = claims.claim(task.orderId(), "two").orElseThrow(); claims.failed(second, "still unavailable");
        assertThat(store.find(task.orderId()).status()).isEqualTo("NEEDS_REVIEW");
        assertThat(claims.candidates(100)).doesNotContain(task.orderId());
        lifecycle.retry(task.orderId(), "test-admin");
        FulfillmentTask retry = claims.claim(task.orderId(), "three").orElseThrow();
        assertThat(retry.requestKey()).isEqualTo(first.requestKey());
        assertThat(retry.commandJson()).isEqualTo(first.commandJson());
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_attempt WHERE order_id=? AND outcome='MANUAL_RETRY'", Integer.class, task.orderId())).isEqualTo(1);
    }

    @Test void receiptMustMatchCommandAndReceiptConfirmationDoesNotDeductStockAgain() {
        FulfillmentTask task = paid();
        int available = inventory.getStock("PRODUCT", 1001L).availableQuantity();
        FulfillmentTask claim = claims.claim(task.orderId(), "worker").orElseThrow();
        assertThatThrownBy(() -> claims.shipped(claim, new WarehouseReceipt(claim.requestKey(), claim.orderId(), "wrong", "tracking")))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> orders.complete(task.orderId(), user)).isInstanceOf(BusinessConflictException.class);
        assertThat(claims.shipped(claim, receipt(claim))).isTrue();
        assertThat(orders.complete(task.orderId(), user).status()).isEqualTo("COMPLETED");
        assertThat(lifecycle.get(task.orderId(), user).status()).isEqualTo("RECEIVED");
        assertThat(inventory.getStock("PRODUCT", 1001L).availableQuantity()).isEqualTo(available);
        assertThatThrownBy(() -> lifecycle.get(task.orderId(), "other-user")).hasMessageContaining("无权");
    }

    @Test void reconciliationRecoversPaidEnrollmentWithoutFabricatingInboxReceipt() {
        String id = order(); orders.markPaid(id);
        assertThat(store.find(id).status()).isEqualTo("WAITING_PAYMENT");
        events.reconcile(id);
        assertThat(store.find(id).status()).isEqualTo("READY");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM inbox_event WHERE consumer_name='fulfillment-v1'", Integer.class)).isZero();
    }

    @Test void deadLetterRecoveryPreservesOriginalEvidenceAndRevalidatesTheEvent() throws Exception {
        String id = order(); orders.markPaid(id);
        String value = json.writeValueAsString(event(id));
        deadLetters.record("fixture", 0, 42, value, new IllegalStateException("database temporarily unavailable"));
        deadLetters.record("fixture", 0, 42, value, new IllegalStateException("recovery replay"));
        String key = UUID.nameUUIDFromBytes("fixture/0/42".getBytes(java.nio.charset.StandardCharsets.UTF_8)).toString();
        deadLetters.replay(key, "admin-fixture");
        assertThat(store.find(id).status()).isEqualTo("READY");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM dead_letter_event WHERE source='FULFILLMENT_KAFKA' AND event_id=?", Integer.class, key)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM fulfillment_attempt WHERE order_id=? AND outcome='EVENT_REPLAY'", Integer.class, id)).isEqualTo(1);
    }
}
