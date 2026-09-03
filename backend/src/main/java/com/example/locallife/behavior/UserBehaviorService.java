package com.example.locallife.behavior;

import com.example.locallife.common.ResourceNotFoundException;
import com.example.locallife.shop.ShopRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;

@Service
public class UserBehaviorService {

    private static final int DEFAULT_HISTORY_LIMIT = 20;
    private static final int MAX_HISTORY_LIMIT = 100;

    private final UserBehaviorRepository userBehaviorRepository;
    private final ShopRepository shopRepository;

    public UserBehaviorService(
            UserBehaviorRepository userBehaviorRepository,
            ShopRepository shopRepository
    ) {
        this.userBehaviorRepository = userBehaviorRepository;
        this.shopRepository = shopRepository;
    }

    @Transactional
    public UserBehaviorResponse createBehavior(
            CreateUserBehaviorRequest request,
            String effectiveUserId
    ) {
        shopRepository.findById(request.shopId())
                .orElseThrow(() -> new ResourceNotFoundException("商户不存在"));

        String behaviorId = "behavior-" + UUID.randomUUID();
        LocalDateTime occurredAt = request.occurredAt() == null
                ? LocalDateTime.now()
                : request.occurredAt();

        UserBehavior behavior = new UserBehavior(
                behaviorId,
                effectiveUserId,
                request.shopId(),
                request.behaviorType().trim(),
                request.score(),
                request.source().trim(),
                occurredAt,
                null
        );
        userBehaviorRepository.insert(behavior);

        return userBehaviorRepository.findById(behaviorId)
                .map(this::toResponse)
                .orElseThrow(() -> new IllegalStateException("新增用户行为后无法查询行为"));
    }

    public List<UserBehaviorResponse> listUserBehaviors(String userId, Integer limit) {
        int effectiveLimit = normalizeLimit(limit);
        return userBehaviorRepository.findByUserId(userId.trim(), effectiveLimit).stream()
                .map(this::toResponse)
                .toList();
    }

    private int normalizeLimit(Integer limit) {
        if (limit == null) {
            return DEFAULT_HISTORY_LIMIT;
        }
        if (limit <= 0) {
            return DEFAULT_HISTORY_LIMIT;
        }
        return Math.min(limit, MAX_HISTORY_LIMIT);
    }

    private UserBehaviorResponse toResponse(UserBehavior behavior) {
        return new UserBehaviorResponse(
                behavior.id(),
                behavior.userId(),
                behavior.shopId(),
                behavior.behaviorType(),
                behavior.score(),
                behavior.source(),
                behavior.sourceUserId(),
                behavior.sourceReviewId(),
                behavior.occurredAt()
        );
    }
}
