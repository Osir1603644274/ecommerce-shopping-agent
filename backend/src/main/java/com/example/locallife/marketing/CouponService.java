package com.example.locallife.marketing;

import com.example.locallife.common.BusinessConflictException;
import com.example.locallife.common.InvalidBusinessStateException;
import com.example.locallife.common.ResourceNotFoundException;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;

@Service
public class CouponService {
    private final CouponMapper mapper;
    private final Clock clock;

    @Autowired
    public CouponService(CouponMapper mapper) {
        this(mapper, Clock.systemUTC());
    }

    CouponService(CouponMapper mapper, Clock clock) {
        this.mapper = mapper;
        this.clock = clock;
    }

    @Transactional
    public CouponTemplate createTemplate(CreateCouponTemplateRequest request) {
        if (!request.validUntil().isAfter(request.validFrom())) {
            throw new InvalidBusinessStateException("优惠券结束时间必须晚于开始时间");
        }
        CouponMapper.MutableCouponTemplate mutable = new CouponMapper.MutableCouponTemplate(request);
        mapper.insertTemplate(mutable);
        return requireTemplate(mutable.getId());
    }

    @Transactional
    public UserCoupon claim(Long templateId, String userId) {
        LocalDateTime now = LocalDateTime.now(clock);
        if (mapper.claimQuota(templateId, now) != 1) {
            throw new BusinessConflictException("优惠券不存在、未生效、已过期或已领完");
        }
        String id = UUID.randomUUID().toString();
        try {
            mapper.insertUserCoupon(id, templateId, userId, now);
        } catch (DuplicateKeyException exception) {
            throw new BusinessConflictException("每位用户只能领取一次该优惠券");
        }
        return requireUserCoupon(id);
    }

    public List<UserCoupon> listMine(String userId) {
        return mapper.findUserCoupons(userId);
    }

    @Transactional
    public long consume(String couponId, String userId, String orderId, long totalMinor) {
        if (couponId == null) {
            return 0;
        }
        CouponQuote quote = quote(couponId, userId, totalMinor);
        LocalDateTime now = LocalDateTime.now(clock);
        if (mapper.consume(couponId, userId, orderId, now) != 1) {
            throw new BusinessConflictException("优惠券已被使用");
        }
        return quote.discountMinor();
    }

    public CouponQuote quote(String couponId, String userId, long totalMinor) {
        if (couponId == null) {
            return new CouponQuote(null, 0L);
        }
        UserCoupon coupon = requireUserCoupon(couponId);
        CouponTemplate template = requireTemplate(coupon.templateId());
        LocalDateTime now = LocalDateTime.now(clock);
        if (!userId.equals(coupon.userId())
                || !"AVAILABLE".equals(coupon.status())
                || !"ACTIVE".equals(template.status())
                || now.isBefore(template.validFrom())
                || !now.isBefore(template.validUntil())
                || totalMinor < template.thresholdMinor()) {
            throw new InvalidBusinessStateException("优惠券不可用于当前订单");
        }
        return new CouponQuote(couponId, Math.min(template.discountMinor(), totalMinor));
    }

    @Transactional
    public void releaseByOrder(String orderId) {
        mapper.releaseByOrder(orderId);
    }

    private CouponTemplate requireTemplate(Long id) {
        CouponTemplate template = mapper.findTemplate(id);
        if (template == null) {
            throw new ResourceNotFoundException("优惠券模板不存在");
        }
        return template;
    }

    private UserCoupon requireUserCoupon(String id) {
        UserCoupon coupon = mapper.findUserCoupon(id);
        if (coupon == null) {
            throw new ResourceNotFoundException("用户优惠券不存在");
        }
        return coupon;
    }

    public record CouponQuote(String couponId, long discountMinor) {
    }
}
