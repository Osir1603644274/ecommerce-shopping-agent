package com.example.locallife.ordering;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.ForbiddenOperationException;
import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.common.ResourceNotFoundException;
import com.example.locallife.inventory.InventoryService;
import com.example.locallife.integration.DomainEventTypes;
import com.example.locallife.integration.OutboxService;
import com.example.locallife.marketing.CouponService;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

@Service
public class OrderService {
    private static final DateTimeFormatter ORDER_TIME = DateTimeFormatter.ofPattern("yyyyMMddHHmmss");
    private final OrderMapper mapper;
    private final CommerceCatalogPort catalog;
    private final InventoryService inventoryService;
    private final CouponService couponService;
    private final OutboxService outboxService;
    private final Clock clock;

    @Autowired
    public OrderService(
            OrderMapper mapper,
            CommerceCatalogPort catalog,
            InventoryService inventoryService,
            CouponService couponService,
            OutboxService outboxService
    ) {
        this(mapper, catalog, inventoryService, couponService,
                outboxService, Clock.systemUTC());
    }

    OrderService(
            OrderMapper mapper,
            CommerceCatalogPort catalog,
            InventoryService inventoryService,
            CouponService couponService,
            OutboxService outboxService,
            Clock clock
    ) {
        this.mapper = mapper;
        this.catalog = catalog;
        this.inventoryService = inventoryService;
        this.couponService = couponService;
        this.outboxService = outboxService;
        this.clock = clock;
    }

    @Transactional
    public OrderResponse create(
            CreateOrderRequest request,
            String userId,
            String idempotencyKey
    ) {
        String normalizedKey = normalizeIdempotencyKey(idempotencyKey);
        String requestHash = requestHash(request);
        CustomerOrder existing = mapper.findByIdempotencyKey(userId, normalizedKey);
        if (existing != null) {
            if (!requestHash.equals(existing.requestHash())) {
                throw new BusinessConflictException("同一 Idempotency-Key 不能用于不同的下单参数");
            }
            return toResponse(existing);
        }

        ResolvedItem resolved = resolveItem(request.itemType(), request.itemId());
        long total = Math.multiplyExact(resolved.unitPriceMinor(), request.quantity().longValue());
        String orderId = UUID.randomUUID().toString();
        LocalDateTime now = LocalDateTime.now(clock);
        LocalDateTime expiresAt = now.plusMinutes(15);
        long discount = couponService.consume(
                blankToNull(request.userCouponId()), userId, orderId, total);
        CustomerOrder order = new CustomerOrder(
                orderId,
                newOrderNo(now),
                userId,
                normalizedKey,
                requestHash,
                OrderStatus.PENDING_PAYMENT.name(),
                total,
                discount,
                total - discount,
                "CNY",
                blankToNull(request.userCouponId()),
                expiresAt,
                null,
                null,
                null,
                0L,
                null,
                null
        );
        try {
            mapper.insertOrder(order);
        } catch (DuplicateKeyException exception) {
            throw new BusinessConflictException("相同幂等键的订单正在创建，请重试查询");
        }
        mapper.insertItem(new OrderItem(
                null,
                orderId,
                request.itemType(),
                request.itemId(),
                resolved.title(),
                resolved.unitPriceMinor(),
                request.quantity(),
                total,
                resolved.evidenceJson()
        ));
        inventoryService.reserve(
                orderId, request.itemType(), request.itemId(), request.quantity(), expiresAt);
        appendOrderEvent(orderId, DomainEventTypes.ORDER_CREATED_V1);
        return toResponse(requireOrder(orderId));
    }

    @Transactional(readOnly = true)
    public OrderPreviewResponse preview(CreateOrderRequest request, String userId) {
        ResolvedItem resolved = resolveItem(request.itemType(), request.itemId());
        long total = Math.multiplyExact(
                resolved.unitPriceMinor(), request.quantity().longValue());
        long discount = couponService.quote(
                blankToNull(request.userCouponId()), userId, total).discountMinor();
        var stock = inventoryService.getStock(request.itemType(), request.itemId());
        if (stock.availableQuantity() < request.quantity()) {
            throw new BusinessConflictException("库存不足");
        }
        return new OrderPreviewResponse(
                request.itemType(),
                request.itemId(),
                resolved.title(),
                resolved.unitPriceMinor(),
                request.quantity(),
                total,
                discount,
                total - discount,
                "CNY",
                blankToNull(request.userCouponId()),
                stock.availableQuantity(),
                resolved.evidenceJson()
        );
    }

    public OrderResponse get(String orderId, String actorUserId, boolean privileged) {
        CustomerOrder order = requireOrder(orderId);
        requireOwner(order, actorUserId, privileged);
        return toResponse(order);
    }

    @Transactional(readOnly = true)
    public OrderResponse getByIdempotencyKey(String idempotencyKey, String userId) {
        CustomerOrder order = mapper.findByIdempotencyKey(
                userId, normalizeIdempotencyKey(idempotencyKey));
        if (order == null) {
            throw new ResourceNotFoundException("幂等键对应的订单不存在");
        }
        return toResponse(order);
    }

    public List<OrderResponse> listMine(String userId) {
        return mapper.findByUserId(userId).stream().map(this::toResponse).toList();
    }

    @Transactional
    public OrderResponse cancel(String orderId, String actorUserId, boolean privileged) {
        CustomerOrder order = requireOrder(orderId);
        requireOwner(order, actorUserId, privileged);
        if (OrderStatus.CANCELLED.name().equals(order.status())) {
            return toResponse(order);
        }
        if (mapper.transition(orderId, OrderStatus.PENDING_PAYMENT.name(),
                OrderStatus.CANCELLED.name(), LocalDateTime.now(clock)) != 1) {
            throw new InvalidBusinessStateException("只有待支付订单可以取消");
        }
        inventoryService.release(orderId, false);
        couponService.releaseByOrder(orderId);
        appendOrderEvent(orderId, DomainEventTypes.ORDER_CANCELLED_V1);
        return toResponse(requireOrder(orderId));
    }

    @Transactional
    public OrderResponse complete(String orderId, String actorUserId) {
        CustomerOrder order = requireOrder(orderId);
        requireOwner(order, actorUserId, false);
        transition(orderId, OrderStatus.PAID, OrderStatus.COMPLETED, "只有已支付订单可以完成");
        return toResponse(requireOrder(orderId));
    }

    @Transactional
    public void markPaid(String orderId) {
        CustomerOrder order = requireOrder(orderId);
        if (OrderStatus.PAID.name().equals(order.status())) {
            return;
        }
        transition(orderId, OrderStatus.PENDING_PAYMENT, OrderStatus.PAID, "订单已关闭，不能支付");
        inventoryService.confirm(orderId);
        appendOrderEvent(orderId, DomainEventTypes.ORDER_PAID_V1);
    }

    @Transactional
    public void markRefunding(String orderId) {
        transition(orderId, OrderStatus.PAID, OrderStatus.REFUNDING, "只有已支付订单可以退款");
    }

    @Transactional
    public void markRefunded(String orderId) {
        transition(orderId, OrderStatus.REFUNDING, OrderStatus.REFUNDED, "订单不在退款中");
        appendOrderEvent(orderId, DomainEventTypes.ORDER_REFUNDED_V1);
    }

    @Transactional
    public int expireBatch(int limit) {
        LocalDateTime now = LocalDateTime.now(clock);
        int expired = 0;
        for (String orderId : mapper.findExpiredIds(now, Math.max(1, Math.min(limit, 500)))) {
            if (mapper.transition(orderId, OrderStatus.PENDING_PAYMENT.name(),
                    OrderStatus.EXPIRED.name(), now) == 1) {
                inventoryService.release(orderId, true);
                couponService.releaseByOrder(orderId);
                appendOrderEvent(orderId, DomainEventTypes.ORDER_EXPIRED_V1);
                expired++;
            }
        }
        return expired;
    }

    public CustomerOrder requireOwnedOrder(String orderId, String userId) {
        CustomerOrder order = requireOrder(orderId);
        requireOwner(order, userId, false);
        return order;
    }

    private ResolvedItem resolveItem(String itemType, Long itemId) {
        CommerceItemSnapshot item = catalog.requireItem(itemType, itemId);
        return new ResolvedItem(item.title(), item.unitPriceMinor(), item.evidenceJson());
    }

    private void transition(
            String orderId,
            OrderStatus expected,
            OrderStatus target,
            String message
    ) {
        if (mapper.transition(orderId, expected.name(), target.name(), LocalDateTime.now(clock)) != 1) {
            throw new InvalidBusinessStateException(message);
        }
    }

    private OrderResponse toResponse(CustomerOrder order) {
        return new OrderResponse(
                order.id(), order.orderNo(), order.status(), order.totalMinor(),
                order.discountMinor(), order.payableMinor(), order.currency(),
                order.userCouponId(), order.expiresAt(), order.paidAt(), order.createdAt(),
                mapper.findItems(order.id())
        );
    }

    private CustomerOrder requireOrder(String orderReference) {
        CustomerOrder order = mapper.findByReference(orderReference);
        if (order == null) {
            throw new ResourceNotFoundException("订单不存在");
        }
        return order;
    }

    private static void requireOwner(
            CustomerOrder order,
            String actorUserId,
            boolean privileged
    ) {
        if (!privileged && !order.userId().equals(actorUserId)) {
            throw new ForbiddenOperationException("无权访问该订单");
        }
    }

    private static String normalizeIdempotencyKey(String key) {
        if (key == null || key.isBlank() || key.length() > 128) {
            throw new InvalidBusinessStateException("Idempotency-Key 必填且不能超过 128 字符");
        }
        return key.strip();
    }

    private static String newOrderNo(LocalDateTime now) {
        return "O" + ORDER_TIME.format(now)
                + UUID.randomUUID().toString().replace("-", "").substring(0, 12).toUpperCase(Locale.ROOT);
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value.strip();
    }

    private static String safe(String value) {
        return value == null ? "" : value;
    }

    private void appendOrderEvent(String orderId, String eventType) {
        CustomerOrder order = requireOrder(orderId);
        outboxService.append(
                "ORDER",
                order.id(),
                eventType,
                java.util.Map.of(
                        "orderId", order.id(),
                        "orderNo", order.orderNo(),
                        "userId", order.userId(),
                        "status", order.status(),
                        "payableMinor", order.payableMinor(),
                        "currency", order.currency()
                )
        );
    }

    private static String requestHash(CreateOrderRequest request) {
        String canonical = String.join("|",
                request.itemType(),
                request.itemId().toString(),
                request.quantity().toString(),
                safe(blankToNull(request.userCouponId())));
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(canonical.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException("无法生成订单请求摘要", exception);
        }
    }

    private record ResolvedItem(String title, long unitPriceMinor, String evidenceJson) {
    }
}
