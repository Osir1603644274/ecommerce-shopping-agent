package com.example.locallife.ordering;

import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.inventory.InventoryStock;
import com.example.locallife.marketing.CouponService;
import com.example.locallife.marketing.CreateCouponTemplateRequest;
import com.example.locallife.marketing.UserCoupon;
import com.example.locallife.review.ReviewVectorSyncClient;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;
import java.time.LocalDateTime;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest
@Transactional
class OrderServiceIntegrationTests {
    @Autowired
    private CartOrderService cartOrderService;
    @Autowired
    private OrderService orderService;

    @Autowired
    private InventoryService inventoryService;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Autowired
    private CouponService couponService;

    @MockitoBean
    private ReviewVectorSyncClient reviewVectorSyncClient;

    private String userId;

    @BeforeEach
    void setUp() {
        userId = UUID.randomUUID().toString();
        jdbcTemplate.update("""
                INSERT INTO user_account(id, username, password_hash, enabled, token_version)
                VALUES(?, ?, 'unused', TRUE, 0)
                """, userId, "order-" + UUID.randomUUID());
        inventoryService.createStock("PRODUCT", 1001L, 5);
    }

    @Test
    void disabledFulfillmentRejectsNewCartBeforeAnyOrderOrStockChange() {
        var request = new CreateCartOrderRequest(java.util.List.of(
                new CreateCartOrderRequest.Line("PRODUCT", 1001L, 1)), null);
        assertThatThrownBy(() -> cartOrderService.create(request, userId, "cart-disabled"))
                .isInstanceOf(com.example.locallife.common.BusinessConflictException.class)
                .hasMessageContaining("先启用可靠履约");
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM customer_order WHERE user_id=?", Integer.class, userId)).isZero();
        assertThat(inventoryService.getStock("PRODUCT", 1001L).availableQuantity()).isEqualTo(5);
        assertThat(inventoryService.getStock("PRODUCT", 1001L).reservedQuantity()).isZero();
    }

    @Test
    void sameIdempotencyKeyReturnsOriginalOrderWithoutDoubleReservation() {
        CreateOrderRequest request = new CreateOrderRequest("PRODUCT", 1001L, 2, null);

        OrderResponse first = orderService.create(request, userId, "checkout-1");
        OrderResponse replay = orderService.create(request, userId, "checkout-1");

        assertThat(replay.id()).isEqualTo(first.id());
        assertThat(replay.status()).isEqualTo(OrderStatus.PENDING_PAYMENT.name());
        assertThat(replay.payableMinor()).isEqualTo(499800L);
        assertThat(replay.items()).singleElement()
                .satisfies(item -> {
                    assertThat(item.unitPriceMinor()).isEqualTo(249900L);
                    assertThat(item.evidenceJson()).contains("\"priceStatus\":\"verified\"");
                });
        InventoryStock stock = inventoryService.getStock("PRODUCT", 1001L);
        assertThat(stock.availableQuantity()).isEqualTo(3);
        assertThat(stock.reservedQuantity()).isEqualTo(2);
    }

    @Test
    void changedConfirmationPriceRejectsWithoutOrderOrInventoryEffect() {
        var wrongPrice = new CreateOrderRequest("PRODUCT", 1001L, 1, null, 1L, 1L);
        assertThatThrownBy(() -> orderService.create(wrongPrice, userId, "changed-price"))
                .hasMessageContaining("价格已变化");
        assertThat(inventoryService.getStock("PRODUCT", 1001L).availableQuantity()).isEqualTo(5);
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM customer_order WHERE user_id=?", Integer.class, userId)).isZero();
    }

    @Test
    void confirmationAmountParticipatesInIdempotencyIdentity() {
        var request = new CreateOrderRequest("PRODUCT", 1001L, 1, null, 249900L, 249900L);
        var first = orderService.create(request, userId, "confirmed-price");
        assertThat(orderService.create(request, userId, "confirmed-price").id()).isEqualTo(first.id());
        assertThatThrownBy(() -> orderService.create(
                new CreateOrderRequest("PRODUCT", 1001L, 1, null, 249900L, 249899L), userId, "confirmed-price"))
                .hasMessageContaining("不能用于不同");
        assertThat(inventoryService.getStock("PRODUCT", 1001L).availableQuantity()).isEqualTo(4);
    }

    @Test
    void previewUsesAuthoritativePriceWithoutReservingStockOrCreatingOrder() {
        CreateOrderRequest request = new CreateOrderRequest("PRODUCT", 1001L, 2, null);

        OrderPreviewResponse preview = orderService.preview(request, userId);

        assertThat(preview.title()).isEqualTo(
                "远航 P1 5G 手机 12GB+256GB 5000mAh");
        assertThat(preview.unitPriceMinor()).isEqualTo(249900L);
        assertThat(preview.totalMinor()).isEqualTo(499800L);
        assertThat(preview.payableMinor()).isEqualTo(499800L);
        assertThat(preview.availableQuantity()).isEqualTo(5);
        assertThat(preview.priceEvidence()).contains("\"priceStatus\":\"verified\"");
        assertThat(orderService.listMine(userId)).isEmpty();
        InventoryStock stock = inventoryService.getStock("PRODUCT", 1001L);
        assertThat(stock.availableQuantity()).isEqualTo(5);
        assertThat(stock.reservedQuantity()).isZero();
    }

    @Test
    void sameIdempotencyKeyRejectsDifferentRequestBody() {
        orderService.create(
                new CreateOrderRequest("PRODUCT", 1001L, 1, null),
                userId,
                "checkout-conflict"
        );

        assertThatThrownBy(() -> orderService.create(
                new CreateOrderRequest("PRODUCT", 1001L, 2, null),
                userId,
                "checkout-conflict"
        )).isInstanceOf(com.example.locallife.common.BusinessConflictException.class)
                .hasMessageContaining("不同的下单参数");
    }

    @Test
    void cancellationReleasesReservationAndIsIdempotent() {
        OrderResponse created = orderService.create(
                new CreateOrderRequest("PRODUCT", 1001L, 1, null),
                userId,
                "checkout-cancel"
        );

        OrderResponse cancelled = orderService.cancel(created.id(), userId, false);
        OrderResponse replay = orderService.cancel(created.id(), userId, false);

        assertThat(cancelled.status()).isEqualTo(OrderStatus.CANCELLED.name());
        assertThat(replay.status()).isEqualTo(OrderStatus.CANCELLED.name());
        InventoryStock stock = inventoryService.getStock("PRODUCT", 1001L);
        assertThat(stock.availableQuantity()).isEqualTo(5);
        assertThat(stock.reservedQuantity()).isZero();
    }

    @Test
    void expiryReleasesReservationPublishesOneOutboxEventAndReplayIsNoOp() {
        OrderResponse created = orderService.create(
                new CreateOrderRequest("PRODUCT", 1001L, 2, null),
                userId,
                "checkout-expire"
        );
        jdbcTemplate.update(
                "UPDATE customer_order SET expires_at = ? WHERE id = ?",
                LocalDateTime.now(ZoneOffset.UTC).minusMinutes(1),
                created.id()
        );

        assertThat(orderService.expireBatch(20)).isEqualTo(1);
        assertThat(orderService.expireBatch(20)).isZero();

        assertThat(orderService.get(created.id(), userId, false).status())
                .isEqualTo(OrderStatus.EXPIRED.name());
        InventoryStock stock = inventoryService.getStock("PRODUCT", 1001L);
        assertThat(stock.availableQuantity()).isEqualTo(5);
        assertThat(stock.reservedQuantity()).isZero();
        assertThat(jdbcTemplate.queryForObject(
                "SELECT status FROM inventory_reservation WHERE order_id = ?",
                String.class,
                created.id()
        )).isEqualTo("EXPIRED");
        assertThat(jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM outbox_event WHERE aggregate_id = ? AND event_type = ?",
                Integer.class,
                created.id(),
                "order.expired.v1"
        )).isEqualTo(1);
    }

    @Test
    void unverifiedProductPriceCannotBecomeTransactionPrice() {
        assertThatThrownBy(() -> orderService.create(
                new CreateOrderRequest("PRODUCT", 1002L, 1, null),
                userId,
                "checkout-unverified"
        )).isInstanceOf(InvalidBusinessStateException.class)
                .hasMessageContaining("已验证快照价格");
    }

    @Test
    void couponIsConsumedAtomicallyAndReleasedWhenOrderIsCancelled() {
        LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);
        var template = couponService.createTemplate(new CreateCouponTemplateRequest(
                "满 2000 减 100",
                200000L,
                10000L,
                10,
                now.minusMinutes(1),
                now.plusDays(1)
        ));
        UserCoupon coupon = couponService.claim(template.id(), userId);

        OrderResponse created = orderService.create(
                new CreateOrderRequest("PRODUCT", 1001L, 1, coupon.id()),
                userId,
                "checkout-coupon"
        );

        assertThat(created.discountMinor()).isEqualTo(10000L);
        assertThat(created.payableMinor()).isEqualTo(239900L);
        assertThat(couponService.listMine(userId)).singleElement()
                .satisfies(current -> assertThat(current.status()).isEqualTo("USED"));

        orderService.cancel(created.id(), userId, false);
        assertThat(couponService.listMine(userId)).singleElement()
                .satisfies(current -> assertThat(current.status()).isEqualTo("AVAILABLE"));
    }

    @Test
    void previewQuotesCouponWithoutConsumingIt() {
        LocalDateTime now = LocalDateTime.now(ZoneOffset.UTC);
        var template = couponService.createTemplate(new CreateCouponTemplateRequest(
                "满 2000 减 100",
                200000L,
                10000L,
                10,
                now.minusMinutes(1),
                now.plusDays(1)
        ));
        UserCoupon coupon = couponService.claim(template.id(), userId);

        OrderPreviewResponse preview = orderService.preview(
                new CreateOrderRequest("PRODUCT", 1001L, 1, coupon.id()),
                userId
        );

        assertThat(preview.discountMinor()).isEqualTo(10000L);
        assertThat(preview.payableMinor()).isEqualTo(239900L);
        assertThat(couponService.listMine(userId)).singleElement()
                .satisfies(current -> assertThat(current.status()).isEqualTo("AVAILABLE"));
        assertThat(orderService.listMine(userId)).isEmpty();
    }
}
