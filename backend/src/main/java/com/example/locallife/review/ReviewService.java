package com.example.locallife.review;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.example.locallife.common.ResourceNotFoundException;
import com.example.locallife.common.ForbiddenOperationException;
import com.example.locallife.integration.DomainEventTypes;
import com.example.locallife.integration.OutboxService;
import com.example.locallife.shop.Shop;
import com.example.locallife.shop.ShopRepository;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.LinkedHashMap;
import java.util.function.Function;
import java.util.stream.Collectors;

@Service
public class ReviewService {

    private final ReviewRepository reviewRepository;
    private final ShopRepository shopRepository;
    private final ReviewVectorSyncClient reviewVectorSyncClient;
    private final OutboxService outboxService;
    private final ObjectMapper objectMapper;
    private final String syncMode;

    public ReviewService(
            ReviewRepository reviewRepository,
            ShopRepository shopRepository,
            ReviewVectorSyncClient reviewVectorSyncClient,
            OutboxService outboxService,
            ObjectMapper objectMapper,
            @Value("${agent.review-sync.mode:outbox}") String syncMode
    ) {
        this.reviewRepository = reviewRepository;
        this.shopRepository = shopRepository;
        this.reviewVectorSyncClient = reviewVectorSyncClient;
        this.outboxService = outboxService;
        this.objectMapper = objectMapper;
        this.syncMode = syncMode;
    }

    public List<ReviewResponse> listReviews(Long shopId) {
        List<Review> reviews = shopId == null
                ? reviewRepository.findAll()
                : reviewRepository.findByShopId(shopId);
        Map<Long, Shop> shopsById = shopRepository.findAll().stream()
                .collect(Collectors.toMap(Shop::id, Function.identity()));

        return reviews.stream()
                .map(review -> toResponse(review, requireShop(shopsById, review.shopId())))
                .toList();
    }

    @Transactional
    public ReviewResponse createReview(CreateReviewRequest request, String actorUserId) {
        Shop shop = shopRepository.findById(request.shopId())
                .orElseThrow(() -> new ResourceNotFoundException("商户不存在"));

        String reviewId = "review-" + UUID.randomUUID();
        String content = request.content().trim();
        List<String> tags = request.tags().stream()
                .map(String::trim)
                .distinct()
                .toList();

        reviewRepository.insert(
                reviewId,
                request.shopId(),
                actorUserId,
                content,
                serializeTags(tags)
        );

        ReviewResponse response = reviewRepository.findById(reviewId)
                .map(review -> toResponse(review, shop))
                .orElseThrow(() -> new IllegalStateException("新增评论后无法查询评论"));
        publishUpsert(response, shop.name());
        return response;
    }

    @Transactional
    public ReviewResponse updateReview(
            String reviewId,
            UpdateReviewRequest request,
            String actorUserId,
            boolean admin
    ) {
        Review existingReview = reviewRepository.findById(reviewId)
                .orElseThrow(() -> new ResourceNotFoundException("评论不存在"));
        requireOwnerOrAdmin(existingReview, actorUserId, admin);
        Shop shop = shopRepository.findById(existingReview.shopId())
                .orElseThrow(() -> new ResourceNotFoundException("商户不存在"));

        String content = request.content().trim();
        List<String> tags = request.tags().stream()
                .map(String::trim)
                .distinct()
                .toList();

        reviewRepository.update(reviewId, content, serializeTags(tags));

        ReviewResponse response = reviewRepository.findById(reviewId)
                .map(review -> toResponse(review, shop))
                .orElseThrow(() -> new IllegalStateException("修改评论后无法查询评论"));
        publishUpsert(response, shop.name());
        return response;
    }

    @Transactional
    public void deleteReview(String reviewId, String actorUserId, boolean admin) {
        Review existingReview = reviewRepository.findById(reviewId)
                .orElseThrow(() -> new ResourceNotFoundException("评论不存在"));
        requireOwnerOrAdmin(existingReview, actorUserId, admin);
        int deletedRows = reviewRepository.deleteById(reviewId);
        if (deletedRows == 0) {
            throw new ResourceNotFoundException("评论不存在");
        }
        publishDelete(reviewId);
    }

    private void requireOwnerOrAdmin(Review review, String actorUserId, boolean admin) {
        if (admin) {
            return;
        }
        if (review.ownerUserId() == null || !review.ownerUserId().equals(actorUserId)) {
            throw new ForbiddenOperationException("只能修改或删除自己的评论");
        }
    }

    private ReviewResponse toResponse(Review review, Shop shop) {
        return new ReviewResponse(
                review.id(),
                review.shopId(),
                shop.name(),
                review.content(),
                review.source(),
                review.language(),
                review.contentZh(),
                review.translationStatus(),
                review.sourceReviewId(),
                review.sourceUserId(),
                review.stars(),
                review.sourceShopName(),
                review.evidenceScope(),
                parseTags(review.tags())
        );
    }

    private Shop requireShop(Map<Long, Shop> shopsById, Long shopId) {
        Shop shop = shopsById.get(shopId);
        if (shop == null) {
            throw new IllegalStateException("评论关联的商户不存在");
        }
        return shop;
    }

    private List<String> parseTags(String tags) {
        try {
            return objectMapper.readValue(tags, new TypeReference<List<String>>() {
            });
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("评论标签数据格式错误", exception);
        }
    }

    private String serializeTags(List<String> tags) {
        try {
            return objectMapper.writeValueAsString(tags);
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("评论标签无法保存", exception);
        }
    }

    private void publishUpsert(ReviewResponse review, String shopName) {
        if ("direct".equalsIgnoreCase(syncMode)) {
            reviewVectorSyncClient.upsert(review, shopName);
            return;
        }
        LinkedHashMap<String, Object> payload = new LinkedHashMap<>();
        payload.put("reviewId", review.reviewId());
        payload.put("shopId", review.shopId());
        payload.put("shopName", shopName);
        payload.put("content", review.content());
        payload.put("source", review.source());
        payload.put("language", review.language());
        payload.put("contentZh", review.contentZh());
        payload.put("translationStatus", review.translationStatus());
        payload.put("tags", review.tags());
        outboxService.append(
                "REVIEW",
                review.reviewId(),
                DomainEventTypes.REVIEW_VECTOR_UPSERT_V1,
                payload
        );
    }

    private void publishDelete(String reviewId) {
        if ("direct".equalsIgnoreCase(syncMode)) {
            reviewVectorSyncClient.delete(reviewId);
            return;
        }
        outboxService.append(
                "REVIEW",
                reviewId,
                DomainEventTypes.REVIEW_VECTOR_DELETE_V1,
                java.util.Map.of("reviewId", reviewId)
        );
    }
}
