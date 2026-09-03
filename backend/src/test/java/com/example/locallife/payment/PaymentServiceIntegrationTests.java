package com.example.locallife.payment;

import com.example.locallife.common.ForbiddenOperationException;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.inventory.InventoryStock;
import com.example.locallife.ordering.CreateOrderRequest;
import com.example.locallife.ordering.OrderResponse;
import com.example.locallife.ordering.OrderService;
import com.example.locallife.ordering.OrderStatus;
import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest
@Transactional
class PaymentServiceIntegrationTests {
    @Autowired
    private PaymentService paymentService;

    @Autowired
    private PaymentSignature paymentSignature;

    @Autowired
    private OrderService orderService;

    @Autowired
    private InventoryService inventoryService;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    private String userId;
    private OrderResponse order;

    @BeforeEach
    void setUp() {
        userId = UUID.randomUUID().toString();
        jdbcTemplate.update("""
                INSERT INTO user_account(id, username, password_hash, enabled, token_version)
                VALUES(?, ?, 'unused', TRUE, 0)
                """, userId, "payment-" + UUID.randomUUID());
        inventoryService.createStock("PRODUCT", 1001L, 3);
        order = orderService.create(
                new CreateOrderRequest("PRODUCT", 1001L, 1, null),
                userId,
                "payment-order"
        );
    }

    @Test
    void signedSuccessCallbackIsIdempotentAndConfirmsInventory() {
        PaymentRecord payment = paymentService.create(order.id(), userId);
        PaymentCallbackRequest callback = new PaymentCallbackRequest(
                "event-" + UUID.randomUUID(),
                payment.paymentNo(),
                "provider-trade-1",
                payment.amountMinor(),
                "SUCCESS",
                Instant.now().getEpochSecond()
        );
        String signature = paymentSignature.sign(callback);

        PaymentRecord first = paymentService.processCallback(
                payment.provider(), callback, signature);
        PaymentRecord replay = paymentService.processCallback(
                payment.provider(), callback, signature);

        assertThat(first.status()).isEqualTo("SUCCESS");
        assertThat(replay.id()).isEqualTo(first.id());
        assertThat(orderService.get(order.id(), userId, false).status())
                .isEqualTo(OrderStatus.PAID.name());
        InventoryStock stock = inventoryService.getStock("PRODUCT", 1001L);
        assertThat(stock.reservedQuantity()).isZero();
        assertThat(stock.soldQuantity()).isEqualTo(1);
    }

    @Test
    void duplicateEventIdWithDifferentPayloadIsRejected() {
        PaymentRecord payment = paymentService.create(order.id(), userId);
        String eventId = "event-" + UUID.randomUUID();
        long timestamp = Instant.now().getEpochSecond();
        PaymentCallbackRequest original = new PaymentCallbackRequest(
                eventId,
                payment.paymentNo(),
                "provider-trade-original",
                payment.amountMinor(),
                "SUCCESS",
                timestamp
        );
        paymentService.processCallback(payment.provider(), original, paymentSignature.sign(original));
        PaymentCallbackRequest collision = new PaymentCallbackRequest(
                eventId,
                payment.paymentNo(),
                "provider-trade-collision",
                payment.amountMinor(),
                "SUCCESS",
                timestamp
        );

        assertThatThrownBy(() -> paymentService.processCallback(
                payment.provider(), collision, paymentSignature.sign(collision)))
                .isInstanceOf(ForbiddenOperationException.class)
                .hasMessageContaining("负载不一致");
    }

    @Test
    void callbackRejectsInvalidSignatureBeforeChangingState() {
        PaymentRecord payment = paymentService.create(order.id(), userId);
        PaymentCallbackRequest callback = new PaymentCallbackRequest(
                "event-" + UUID.randomUUID(),
                payment.paymentNo(),
                "provider-trade-bad",
                payment.amountMinor(),
                "SUCCESS",
                Instant.now().getEpochSecond()
        );

        assertThatThrownBy(() -> paymentService.processCallback(
                payment.provider(), callback, "bad-signature"))
                .isInstanceOf(ForbiddenOperationException.class);
        assertThat(paymentService.create(order.id(), userId).status()).isEqualTo("CREATED");
    }

    @Test
    void refundHasExplicitProcessingAndSuccessStates() {
        PaymentRecord payment = paymentService.create(order.id(), userId);
        paymentService.simulateSuccess(payment.id(), userId);

        RefundRecord refund = paymentService.requestRefund(
                order.id(), userId, "商品存在质量问题");
        assertThat(refund.status()).isEqualTo("PROCESSING");
        assertThat(orderService.get(order.id(), userId, false).status())
                .isEqualTo(OrderStatus.REFUNDING.name());

        RefundRecord completed = paymentService.simulateRefundSuccess(refund.id(), userId);
        assertThat(completed.status()).isEqualTo("SUCCESS");
        assertThat(orderService.get(order.id(), userId, false).status())
                .isEqualTo(OrderStatus.REFUNDED.name());
    }
}
