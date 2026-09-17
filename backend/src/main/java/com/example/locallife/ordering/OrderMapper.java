package com.example.locallife.ordering;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;
import java.util.List;

@Mapper
interface OrderMapper {
    String ORDER_COLUMNS = """
            id, order_no, user_id, idempotency_key, request_hash, status, total_minor,
            discount_minor, payable_minor, currency, user_coupon_id, expires_at,
            paid_at, completed_at, cancelled_at, version, created_at, updated_at
            """;

    @Select("SELECT " + ORDER_COLUMNS
            + " FROM customer_order WHERE user_id = #{userId} AND idempotency_key = #{key}")
    CustomerOrder findByIdempotencyKey(
            @Param("userId") String userId,
            @Param("key") String key
    );

    @Select("SELECT " + ORDER_COLUMNS
            + " FROM customer_order WHERE id = #{reference} OR order_no = #{reference} LIMIT 1")
    CustomerOrder findByReference(@Param("reference") String reference);

    @Select("SELECT " + ORDER_COLUMNS
            + " FROM customer_order WHERE user_id = #{userId} ORDER BY created_at DESC")
    List<CustomerOrder> findByUserId(@Param("userId") String userId);

    @Select("""
            <script>
            SELECT """ + " " + ORDER_COLUMNS + """
            FROM customer_order WHERE user_id = #{userId}
            <if test="status != null">AND status = #{status}</if>
            <if test="createdAt != null">
              AND (created_at &lt; #{createdAt} OR (created_at = #{createdAt} AND id &lt; #{id}))
            </if>
            ORDER BY created_at DESC, id DESC LIMIT #{limit}
            </script>
            """)
    List<CustomerOrder> findPage(@Param("userId") String userId, @Param("status") String status,
            @Param("createdAt") LocalDateTime createdAt, @Param("id") String id, @Param("limit") int limit);

    @Select("""
            <script>
            SELECT id, order_id, item_type, item_id, title_snapshot, unit_price_minor,
                   quantity, subtotal_minor, evidence_json
            FROM order_item WHERE order_id IN
            <foreach collection="ids" item="id" open="(" separator="," close=")">#{id}</foreach>
            ORDER BY order_id, id
            </script>
            """)
    List<OrderItem> findItemsForOrders(@Param("ids") List<String> ids);

    @Insert("""
            INSERT INTO customer_order(
                id, order_no, user_id, idempotency_key, request_hash, status, total_minor,
                discount_minor, payable_minor, currency, user_coupon_id, expires_at, version
            ) VALUES(
                #{order.id}, #{order.orderNo}, #{order.userId}, #{order.idempotencyKey},
                #{order.requestHash}, #{order.status}, #{order.totalMinor}, #{order.discountMinor},
                #{order.payableMinor}, #{order.currency}, #{order.userCouponId},
                #{order.expiresAt}, 0
            )
            """)
    int insertOrder(@Param("order") CustomerOrder order);

    @Insert("""
            INSERT INTO order_item(
                order_id, item_type, item_id, title_snapshot, unit_price_minor,
                quantity, subtotal_minor, evidence_json
            ) VALUES(
                #{item.orderId}, #{item.itemType}, #{item.itemId}, #{item.titleSnapshot},
                #{item.unitPriceMinor}, #{item.quantity}, #{item.subtotalMinor}, #{item.evidenceJson}
            )
            """)
    int insertItem(@Param("item") OrderItem item);

    @Select("""
            SELECT id, order_id, item_type, item_id, title_snapshot, unit_price_minor,
                   quantity, subtotal_minor, evidence_json
            FROM order_item WHERE order_id = #{orderId} ORDER BY id
            """)
    List<OrderItem> findItems(@Param("orderId") String orderId);

    @Update("""
            UPDATE customer_order
            SET status = #{target}, version = version + 1,
                paid_at = CASE WHEN #{target} = 'PAID' THEN #{now} ELSE paid_at END,
                completed_at = CASE WHEN #{target} = 'COMPLETED' THEN #{now} ELSE completed_at END,
                cancelled_at = CASE
                    WHEN #{target} IN ('CANCELLED', 'EXPIRED') THEN #{now}
                    ELSE cancelled_at
                END
            WHERE id = #{orderId} AND status = #{expected}
            """)
    int transition(
            @Param("orderId") String orderId,
            @Param("expected") String expected,
            @Param("target") String target,
            @Param("now") LocalDateTime now
    );

    @Select("""
            SELECT id FROM customer_order
            WHERE status = 'PENDING_PAYMENT' AND expires_at <= #{now}
            ORDER BY expires_at LIMIT #{limit}
            """)
    List<String> findExpiredIds(@Param("now") LocalDateTime now, @Param("limit") int limit);
}
