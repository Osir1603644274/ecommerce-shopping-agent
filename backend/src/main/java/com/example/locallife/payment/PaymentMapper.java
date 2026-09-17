package com.example.locallife.payment;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;

@Mapper
interface PaymentMapper {
    @Select("SELECT COUNT(*) FROM order_line_allocation WHERE order_id=#{orderId}")
    int cartLines(@Param("orderId") String orderId);

    String PAYMENT_COLUMNS = """
            id, payment_no, order_id, user_id, provider, status, amount_minor,
            currency, provider_trade_no, paid_at, version, created_at, updated_at
            """;

    @Insert("""
            INSERT INTO payment_record(
                id, payment_no, order_id, user_id, provider, status,
                amount_minor, currency, version
            ) VALUES(
                #{payment.id}, #{payment.paymentNo}, #{payment.orderId}, #{payment.userId},
                #{payment.provider}, #{payment.status}, #{payment.amountMinor},
                #{payment.currency}, 0
            )
            """)
    int insertPayment(@Param("payment") PaymentRecord payment);

    @Select("SELECT " + PAYMENT_COLUMNS + " FROM payment_record WHERE id = #{id}")
    PaymentRecord findById(@Param("id") String id);

    @Select("SELECT " + PAYMENT_COLUMNS + " FROM payment_record WHERE payment_no = #{paymentNo}")
    PaymentRecord findByPaymentNo(@Param("paymentNo") String paymentNo);

    @Select("SELECT " + PAYMENT_COLUMNS + " FROM payment_record WHERE order_id = #{orderId}")
    PaymentRecord findByOrderId(@Param("orderId") String orderId);

    @Update("""
            UPDATE payment_record
            SET status = 'SUCCESS', provider_trade_no = #{providerTradeNo},
                paid_at = #{paidAt}, version = version + 1
            WHERE id = #{id} AND status = 'CREATED'
            """)
    int markSuccess(
            @Param("id") String id,
            @Param("providerTradeNo") String providerTradeNo,
            @Param("paidAt") LocalDateTime paidAt
    );

    @Update("""
            UPDATE payment_record
            SET status = 'FAILED', provider_trade_no = #{providerTradeNo}, version = version + 1
            WHERE id = #{id} AND status = 'CREATED'
            """)
    int markFailed(
            @Param("id") String id,
            @Param("providerTradeNo") String providerTradeNo
    );

    @Insert("""
            INSERT INTO payment_notification(
                provider, event_id, payment_no, payload_hash
            ) VALUES(#{provider}, #{eventId}, #{paymentNo}, #{payloadHash})
            """)
    int insertNotification(
            @Param("provider") String provider,
            @Param("eventId") String eventId,
            @Param("paymentNo") String paymentNo,
            @Param("payloadHash") String payloadHash
    );

    @Select("""
            SELECT payload_hash FROM payment_notification
            WHERE provider = #{provider} AND event_id = #{eventId}
            """)
    String findNotificationHash(
            @Param("provider") String provider,
            @Param("eventId") String eventId
    );

    @Insert("""
            INSERT INTO refund_record(
                id, refund_no, payment_id, order_id, user_id,
                amount_minor, reason, status
            ) VALUES(
                #{refund.id}, #{refund.refundNo}, #{refund.paymentId}, #{refund.orderId},
                #{refund.userId}, #{refund.amountMinor}, #{refund.reason}, #{refund.status}
            )
            """)
    int insertRefund(@Param("refund") RefundRecord refund);

    @Select("""
            SELECT id, refund_no, payment_id, order_id, user_id, amount_minor,
                   reason, status, provider_refund_no, refunded_at, created_at, updated_at
            FROM refund_record WHERE id = #{id}
            """)
    RefundRecord findRefundById(@Param("id") String id);

    @Select("""
            SELECT id, refund_no, payment_id, order_id, user_id, amount_minor,
                   reason, status, provider_refund_no, refunded_at, created_at, updated_at
            FROM refund_record WHERE order_id = #{orderId}
            """)
    RefundRecord findRefundByOrderId(@Param("orderId") String orderId);

    @Update("""
            UPDATE refund_record
            SET status = 'SUCCESS', provider_refund_no = #{providerRefundNo},
                refunded_at = #{refundedAt}, updated_at = CURRENT_TIMESTAMP
            WHERE id = #{id} AND status = 'PROCESSING'
            """)
    int markRefundSuccess(
            @Param("id") String id,
            @Param("providerRefundNo") String providerRefundNo,
            @Param("refundedAt") LocalDateTime refundedAt
    );
}
