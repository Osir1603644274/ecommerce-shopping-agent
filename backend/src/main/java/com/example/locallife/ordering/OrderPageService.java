package com.example.locallife.ordering;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.LocalDateTime;
import java.util.Base64;
import java.util.List;
import java.util.Locale;
import java.util.stream.Collectors;

@Service
public class OrderPageService {
    private final OrderMapper mapper;
    private final byte[] secret;

    public OrderPageService(OrderMapper mapper, @Value("${local-life.auth.secret}") String secret) {
        this.mapper = mapper;
        this.secret = secret.getBytes(StandardCharsets.UTF_8);
    }

    @Transactional(readOnly = true)
    public OrderPage page(String userId, int size, String status, String cursor) {
        if (size < 1 || size > 100) throw badCursor("size 必须在 1 到 100 之间");
        String filter = normalizeStatus(status);
        Position position = decode(cursor, userId, filter);
        List<CustomerOrder> rows = mapper.findPage(userId, filter,
                position == null ? null : position.createdAt(),
                position == null ? null : position.id(), size + 1);
        boolean more = rows.size() > size;
        List<CustomerOrder> selected = rows.subList(0, Math.min(size, rows.size()));
        if (selected.isEmpty()) return new OrderPage(List.of(), null, false);
        var byOrder = mapper.findItemsForOrders(selected.stream().map(CustomerOrder::id).toList())
                .stream().collect(Collectors.groupingBy(OrderItem::orderId));
        List<OrderResponse> responses = selected.stream().map(o -> new OrderResponse(
                o.id(), o.orderNo(), o.status(), o.totalMinor(), o.discountMinor(), o.payableMinor(),
                o.currency(), o.userCouponId(), o.expiresAt(), o.paidAt(), o.createdAt(),
                byOrder.getOrDefault(o.id(), List.of()))).toList();
        CustomerOrder last = selected.get(selected.size() - 1);
        return new OrderPage(responses, more ? encode(last, userId, filter) : null, more);
    }

    private String encode(CustomerOrder order, String user, String status) {
        String payload = "v1|" + order.createdAt() + "|" + order.id();
        String encoded = Base64.getUrlEncoder().withoutPadding()
                .encodeToString(payload.getBytes(StandardCharsets.UTF_8));
        return encoded + "." + Base64.getUrlEncoder().withoutPadding()
                .encodeToString(sign(encoded, user, status));
    }

    private Position decode(String cursor, String user, String status) {
        if (cursor == null || cursor.isBlank()) return null;
        try {
            if (cursor.length() > 512) throw new IllegalArgumentException();
            String[] parts = cursor.split("\\.", -1);
            if (parts.length != 2 || !MessageDigest.isEqual(sign(parts[0], user, status),
                    Base64.getUrlDecoder().decode(parts[1]))) throw new IllegalArgumentException();
            String[] payload = new String(Base64.getUrlDecoder().decode(parts[0]),
                    StandardCharsets.UTF_8).split("\\|", -1);
            if (payload.length != 3 || !"v1".equals(payload[0]) || payload[2].isBlank()
                    || payload[2].length() > 36) throw new IllegalArgumentException();
            return new Position(LocalDateTime.parse(payload[1]), payload[2]);
        } catch (RuntimeException exception) {
            throw badCursor("游标无效，或不属于当前用户与筛选条件");
        }
    }

    private byte[] sign(String payload, String user, String status) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            return mac.doFinal(("order-page-v1\n" + user + "\n" + status + "\n" + payload)
                    .getBytes(StandardCharsets.UTF_8));
        } catch (java.security.GeneralSecurityException e) { throw new IllegalStateException(e); }
    }

    private static String normalizeStatus(String status) {
        if (status == null || status.isBlank()) return null;
        try { return OrderStatus.valueOf(status.strip().toUpperCase(Locale.ROOT)).name(); }
        catch (IllegalArgumentException e) { throw badCursor("未知订单状态"); }
    }

    private static ResponseStatusException badCursor(String message) {
        return new ResponseStatusException(HttpStatus.BAD_REQUEST, message);
    }

    private record Position(LocalDateTime createdAt, String id) { }
}
