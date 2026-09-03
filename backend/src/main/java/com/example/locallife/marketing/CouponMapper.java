package com.example.locallife.marketing;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;
import java.util.List;

@Mapper
interface CouponMapper {

    @Insert("""
            INSERT INTO coupon_template(
                name, threshold_minor, discount_minor, total_quantity,
                claimed_quantity, valid_from, valid_until, status, version
            ) VALUES(
                #{template.name}, #{template.thresholdMinor}, #{template.discountMinor},
                #{template.totalQuantity}, 0, #{template.validFrom}, #{template.validUntil},
                'ACTIVE', 0
            )
            """)
    @Options(useGeneratedKeys = true, keyProperty = "template.id")
    int insertTemplate(@Param("template") MutableCouponTemplate template);

    @Select("""
            SELECT id, name, threshold_minor, discount_minor, total_quantity,
                   claimed_quantity, valid_from, valid_until, status, version
            FROM coupon_template WHERE id = #{id}
            """)
    CouponTemplate findTemplate(@Param("id") Long id);

    @Update("""
            UPDATE coupon_template
            SET claimed_quantity = claimed_quantity + 1, version = version + 1
            WHERE id = #{id}
              AND status = 'ACTIVE'
              AND valid_from <= #{now}
              AND valid_until > #{now}
              AND claimed_quantity < total_quantity
            """)
    int claimQuota(@Param("id") Long id, @Param("now") LocalDateTime now);

    @Insert("""
            INSERT INTO user_coupon(id, template_id, user_id, status, claimed_at, version)
            VALUES(#{id}, #{templateId}, #{userId}, 'AVAILABLE', #{claimedAt}, 0)
            """)
    int insertUserCoupon(
            @Param("id") String id,
            @Param("templateId") Long templateId,
            @Param("userId") String userId,
            @Param("claimedAt") LocalDateTime claimedAt
    );

    @Select("""
            SELECT id, template_id, user_id, status, order_id, claimed_at, used_at, version
            FROM user_coupon WHERE id = #{id}
            """)
    UserCoupon findUserCoupon(@Param("id") String id);

    @Select("""
            SELECT id, template_id, user_id, status, order_id, claimed_at, used_at, version
            FROM user_coupon WHERE user_id = #{userId} ORDER BY claimed_at DESC
            """)
    List<UserCoupon> findUserCoupons(@Param("userId") String userId);

    @Update("""
            UPDATE user_coupon
            SET status = 'USED', order_id = #{orderId}, used_at = #{now}, version = version + 1
            WHERE id = #{couponId} AND user_id = #{userId} AND status = 'AVAILABLE'
            """)
    int consume(
            @Param("couponId") String couponId,
            @Param("userId") String userId,
            @Param("orderId") String orderId,
            @Param("now") LocalDateTime now
    );

    @Update("""
            UPDATE user_coupon
            SET status = 'AVAILABLE', order_id = NULL, used_at = NULL, version = version + 1
            WHERE order_id = #{orderId} AND status = 'USED'
            """)
    int releaseByOrder(@Param("orderId") String orderId);

    final class MutableCouponTemplate {
        private Long id;
        private final String name;
        private final Long thresholdMinor;
        private final Long discountMinor;
        private final Integer totalQuantity;
        private final LocalDateTime validFrom;
        private final LocalDateTime validUntil;

        MutableCouponTemplate(CreateCouponTemplateRequest request) {
            this.name = request.name().strip();
            this.thresholdMinor = request.thresholdMinor();
            this.discountMinor = request.discountMinor();
            this.totalQuantity = request.totalQuantity();
            this.validFrom = request.validFrom();
            this.validUntil = request.validUntil();
        }

        public Long getId() { return id; }
        public void setId(Long id) { this.id = id; }
        public String getName() { return name; }
        public Long getThresholdMinor() { return thresholdMinor; }
        public Long getDiscountMinor() { return discountMinor; }
        public Integer getTotalQuantity() { return totalQuantity; }
        public LocalDateTime getValidFrom() { return validFrom; }
        public LocalDateTime getValidUntil() { return validUntil; }
    }
}
