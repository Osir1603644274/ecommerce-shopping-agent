package com.example.locallife.payment;

import com.example.locallife.integration.OutboxService;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.ordering.CreateOrderRequest;
import com.example.locallife.ordering.OrderService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import java.time.Instant;
import java.util.UUID;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/** Independent audit: intentionally NO test-level transaction, as at an HTTP boundary. */
@SpringBootTest(properties = {"local-life.fulfillment.enabled=true",
        "spring.datasource.url=jdbc:h2:mem:independent_payment_audit;MODE=MySQL;DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1"})
class PaymentBoundaryAuditTests {
    @Autowired PaymentService payments;
    @Autowired PaymentSignature signature;
    @Autowired OrderService orders;
    @Autowired InventoryService inventory;
    @Autowired JdbcTemplate jdbc;
    @MockitoSpyBean OutboxService outbox;
    String user;
    String order;
    PaymentRecord payment;

    @BeforeEach void prepare() {
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isFalse();
        user = UUID.randomUUID().toString();
        jdbc.update("INSERT INTO user_account(id,username,password_hash,enabled,token_version) VALUES(?,?,'x',TRUE,0)", user, user);
        if (jdbc.queryForObject("SELECT COUNT(*) FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=1001", Integer.class) == 0)
            inventory.createStock("PRODUCT", 1001L, 100);
        order = orders.create(new CreateOrderRequest("PRODUCT", 1001L, 1, null), user, UUID.randomUUID().toString()).id();
        payment = payments.create(order, user);
    }

    @Test void simulatorExpiredOrderMustNotPersistSuccessfulPaymentOrNotification() {
        expire();
        assertThatThrownBy(() -> payments.simulateSuccess(payment.id(), user)).hasMessageContaining("订单已关闭");
        record("simulator_expired");
        assertAtomicRejectedPayment("EXPIRED", "EXPIRED");
    }

    @Test void simulatorOutboxFailureMustRollbackPaymentNotificationOrderAndStock() {
        rejectPaidOutbox();
        assertThatThrownBy(() -> payments.simulateSuccess(payment.id(), user)).hasMessageContaining("audit-outbox-failure");
        record("simulator_outbox_failure");
        assertAtomicRejectedPayment("PENDING_PAYMENT", "RESERVED");
    }

    @Test void providerCallbackExpiredOrderControlRollsBackPaymentAndNotification() {
        expire();
        PaymentCallbackRequest request = callback();
        assertThatThrownBy(() -> payments.processCallback(payment.provider(), request, signature.sign(request)))
                .hasMessageContaining("订单已关闭");
        record("provider_expired_control");
        assertAtomicRejectedPayment("EXPIRED", "EXPIRED");
    }

    @Test void providerCallbackOutboxFailureControlRollsBackEntireTransaction() {
        rejectPaidOutbox();
        PaymentCallbackRequest request = callback();
        assertThatThrownBy(() -> payments.processCallback(payment.provider(), request, signature.sign(request)))
                .hasMessageContaining("audit-outbox-failure");
        record("provider_outbox_failure_control");
        assertAtomicRejectedPayment("PENDING_PAYMENT", "RESERVED");
    }

    @Test void refundOutboxFailureRollsBackRestockAndSecondCallbackRestoresOnlyOnce() {
        PaymentCallbackRequest request = callback();
        payments.processCallback(payment.provider(), request, signature.sign(request));
        RefundRecord refund = payments.requestRefund(order, user, "audit refund");
        var before = inventory.getStock("PRODUCT", 1001L);
        doThrow(new IllegalStateException("audit-refund-outbox-failure")).when(outbox)
                .append(eq("ORDER"), eq(order), eq("order.refunded.v1"), any());
        assertThatThrownBy(() -> payments.simulateRefundSuccess(refund.id(), user)).hasMessageContaining("audit-refund-outbox-failure");
        assertThat(jdbc.queryForObject("SELECT status FROM refund_record WHERE id=?", String.class, refund.id())).isEqualTo("PROCESSING");
        assertThat(jdbc.queryForObject("SELECT status FROM customer_order WHERE id=?", String.class, order)).isEqualTo("REFUNDING");
        assertThat(jdbc.queryForObject("SELECT status FROM inventory_reservation WHERE order_id=?", String.class, order)).isEqualTo("CONFIRMED");
        assertThat(inventory.getStock("PRODUCT", 1001L).availableQuantity()).isEqualTo(before.availableQuantity());
        doCallRealMethod().when(outbox).append(eq("ORDER"), eq(order), eq("order.refunded.v1"), any());
        payments.simulateRefundSuccess(refund.id(), user);
        payments.simulateRefundSuccess(refund.id(), user);
        assertThat(inventory.getStock("PRODUCT", 1001L).availableQuantity()).isEqualTo(before.availableQuantity() + 1);
        assertThat(inventory.getStock("PRODUCT", 1001L).soldQuantity()).isEqualTo(before.soldQuantity() - 1);
        record("refund_atomic_control");
    }

    private void expire() {
        jdbc.update("UPDATE customer_order SET expires_at=DATEADD('MINUTE',-1,CURRENT_TIMESTAMP) WHERE id=?", order);
        orders.expireBatch(500);
        assertThat(jdbc.queryForObject("SELECT status FROM customer_order WHERE id=?", String.class, order)).isEqualTo("EXPIRED");
    }
    private void rejectPaidOutbox() {
        doThrow(new IllegalStateException("audit-outbox-failure")).when(outbox)
                .append(eq("ORDER"), eq(order), eq("order.paid.v1"), any());
    }
    private PaymentCallbackRequest callback() {
        return new PaymentCallbackRequest(UUID.randomUUID().toString(), payment.paymentNo(),
                "AUDIT-" + UUID.randomUUID(), payment.amountMinor(), "SUCCESS", Instant.now().getEpochSecond());
    }
    private void assertAtomicRejectedPayment(String expectedOrder, String expectedReservation) {
        assertThat(jdbc.queryForObject("SELECT status FROM customer_order WHERE id=?", String.class, order)).isEqualTo(expectedOrder);
        assertThat(jdbc.queryForObject("SELECT status FROM inventory_reservation WHERE order_id=?", String.class, order)).isEqualTo(expectedReservation);
        assertThat(jdbc.queryForObject("SELECT status FROM payment_record WHERE id=?", String.class, payment.id())).isEqualTo("CREATED");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM payment_notification WHERE payment_no=?", Integer.class, payment.paymentNo())).isZero();
    }
    private void record(String scenario) {
        System.out.println("AUDIT_STATE scenario=" + scenario + " order=" +
                jdbc.queryForObject("SELECT status FROM customer_order WHERE id=?", String.class, order) + " payment=" +
                jdbc.queryForObject("SELECT status FROM payment_record WHERE id=?", String.class, payment.id()) + " reservation=" +
                jdbc.queryForObject("SELECT status FROM inventory_reservation WHERE order_id=?", String.class, order) + " notifications=" +
                jdbc.queryForObject("SELECT COUNT(*) FROM payment_notification WHERE payment_no=?", Integer.class, payment.paymentNo()));
    }
}
