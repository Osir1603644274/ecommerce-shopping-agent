package com.example.locallife.payment;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.ForbiddenOperationException;
import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.common.ResourceNotFoundException;
import com.example.locallife.ordering.CustomerOrder;
import com.example.locallife.ordering.OrderService;
import com.example.locallife.ordering.OrderStatus;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.HexFormat;
import java.util.Locale;
import java.util.UUID;

@Service
public class PaymentService {
    private static final DateTimeFormatter PAYMENT_TIME = DateTimeFormatter.ofPattern("yyyyMMddHHmmss");
    private final PaymentMapper mapper;
    private final OrderService orderService;
    private final PaymentSignature signature;
    private final PaymentProperties properties;
    private final Clock clock;

    @Autowired
    public PaymentService(
            PaymentMapper mapper,
            OrderService orderService,
            PaymentSignature signature,
            PaymentProperties properties
    ) {
        this(mapper, orderService, signature, properties, Clock.systemUTC());
    }

    PaymentService(
            PaymentMapper mapper,
            OrderService orderService,
            PaymentSignature signature,
            PaymentProperties properties,
            Clock clock
    ) {
        this.mapper = mapper;
        this.orderService = orderService;
        this.signature = signature;
        this.properties = properties;
        this.clock = clock;
    }

    @Transactional
    public PaymentRecord create(String orderId, String userId) {
        CustomerOrder order = orderService.requireOwnedOrder(orderId, userId);
        PaymentRecord existing = mapper.findByOrderId(orderId);
        if (existing != null) {
            return existing;
        }
        if (!OrderStatus.PENDING_PAYMENT.name().equals(order.status())
                || !LocalDateTime.now(clock).isBefore(order.expiresAt())) {
            throw new InvalidBusinessStateException("订单当前不可支付");
        }
        PaymentRecord payment = new PaymentRecord(
                UUID.randomUUID().toString(),
                newPaymentNo(LocalDateTime.now(clock)),
                order.id(),
                userId,
                "LOCAL_SIMULATOR",
                "CREATED",
                order.payableMinor(),
                order.currency(),
                null,
                null,
                0L,
                null,
                null
        );
        try {
            mapper.insertPayment(payment);
        } catch (DuplicateKeyException exception) {
            PaymentRecord raced = mapper.findByOrderId(orderId);
            if (raced != null) {
                return raced;
            }
            throw exception;
        }
        return requirePayment(payment.id());
    }

    @Transactional(readOnly = true)
    public PaymentRecord getByOrderId(String orderId, String userId) {
        orderService.requireOwnedOrder(orderId, userId);
        PaymentRecord payment = mapper.findByOrderId(orderId);
        if (payment == null) {
            throw new ResourceNotFoundException("订单对应的支付单不存在");
        }
        return payment;
    }

    @Transactional
    public PaymentRecord processCallback(
            String provider,
            PaymentCallbackRequest request,
            String callbackSignature
    ) {
        signature.verify(request, callbackSignature);
        PaymentRecord payment = requirePaymentNo(request.paymentNo());
        if (!payment.provider().equals(provider)) {
            throw new ForbiddenOperationException("支付渠道不匹配");
        }
        String payloadHash = sha256(request.toString());
        try {
            mapper.insertNotification(provider, request.eventId(), request.paymentNo(), payloadHash);
        } catch (DuplicateKeyException duplicate) {
            String storedHash = mapper.findNotificationHash(provider, request.eventId());
            if (!payloadHash.equals(storedHash)) {
                throw new ForbiddenOperationException("相同支付事件号的通知负载不一致");
            }
            return requirePaymentNo(request.paymentNo());
        }
        if (!payment.amountMinor().equals(request.amountMinor())) {
            throw new ForbiddenOperationException("支付金额不匹配");
        }
        if ("SUCCESS".equals(request.status())) {
            if ("SUCCESS".equals(payment.status())) {
                return payment;
            }
            if (mapper.markSuccess(payment.id(), request.providerTradeNo(),
                    LocalDateTime.ofInstant(Instant.ofEpochSecond(request.timestamp()), ZoneOffset.UTC)) != 1) {
                throw new BusinessConflictException("支付状态已变更");
            }
            orderService.markPaid(payment.orderId());
        } else if ("FAILED".equals(request.status())) {
            mapper.markFailed(payment.id(), request.providerTradeNo());
        } else {
            throw new InvalidBusinessStateException("不支持的支付回调状态");
        }
        return requirePayment(payment.id());
    }

    @Transactional
    public PaymentRecord simulateSuccess(String paymentId, String userId) {
        if (!properties.simulatorEnabled()) {
            throw new ForbiddenOperationException("本地支付模拟器未启用");
        }
        PaymentRecord payment = requirePayment(paymentId);
        if (!payment.userId().equals(userId)) {
            throw new ForbiddenOperationException("无权操作该支付单");
        }
        long timestamp = Instant.now(clock).getEpochSecond();
        PaymentCallbackRequest callback = new PaymentCallbackRequest(
                UUID.randomUUID().toString(),
                payment.paymentNo(),
                "SIM-" + UUID.randomUUID(),
                payment.amountMinor(),
                "SUCCESS",
                timestamp
        );
        return processCallback(payment.provider(), callback, signature.sign(callback));
    }

    @Transactional
    public RefundRecord requestRefund(String orderId, String userId, String reason) {
        CustomerOrder order = orderService.requireOwnedOrder(orderId, userId);
        if (mapper.cartLines(order.id()) != 0) {
            throw new BusinessConflictException("购物车订单请使用按数量退款接口，不能混用整单退款");
        }
        PaymentRecord payment = mapper.findByOrderId(orderId);
        if (payment == null || !"SUCCESS".equals(payment.status())) {
            throw new InvalidBusinessStateException("订单没有可退款的成功支付");
        }
        RefundRecord existing = mapper.findRefundByOrderId(orderId);
        if (existing != null) {
            return existing;
        }
        orderService.markRefunding(order.id());
        RefundRecord refund = new RefundRecord(
                UUID.randomUUID().toString(),
                "R" + newPaymentNo(LocalDateTime.now(clock)).substring(1),
                payment.id(),
                order.id(),
                userId,
                payment.amountMinor(),
                reason.strip(),
                "PROCESSING",
                null,
                null,
                null,
                null
        );
        mapper.insertRefund(refund);
        return requireRefund(refund.id());
    }

    @Transactional
    public RefundRecord simulateRefundSuccess(String refundId, String userId) {
        if (!properties.simulatorEnabled()) {
            throw new ForbiddenOperationException("本地支付模拟器未启用");
        }
        RefundRecord refund = requireRefund(refundId);
        if (!refund.userId().equals(userId)) {
            throw new ForbiddenOperationException("无权操作该退款单");
        }
        if ("SUCCESS".equals(refund.status())) {
            return refund;
        }
        if (mapper.markRefundSuccess(refund.id(), "SIM-R-" + UUID.randomUUID(),
                LocalDateTime.now(clock)) != 1) {
            throw new BusinessConflictException("退款状态已变更");
        }
        orderService.markRefunded(refund.orderId());
        return requireRefund(refund.id());
    }

    private PaymentRecord requirePayment(String id) {
        PaymentRecord payment = mapper.findById(id);
        if (payment == null) {
            throw new ResourceNotFoundException("支付单不存在");
        }
        return payment;
    }

    private PaymentRecord requirePaymentNo(String paymentNo) {
        PaymentRecord payment = mapper.findByPaymentNo(paymentNo);
        if (payment == null) {
            throw new ResourceNotFoundException("支付单不存在");
        }
        return payment;
    }

    private RefundRecord requireRefund(String id) {
        RefundRecord refund = mapper.findRefundById(id);
        if (refund == null) {
            throw new ResourceNotFoundException("退款单不存在");
        }
        return refund;
    }

    private static String newPaymentNo(LocalDateTime now) {
        return "P" + PAYMENT_TIME.format(now)
                + UUID.randomUUID().toString().replace("-", "").substring(0, 12).toUpperCase(Locale.ROOT);
    }

    private static String sha256(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException("无法生成支付回调摘要", exception);
        }
    }
}
