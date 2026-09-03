package com.example.locallife.recommendation;

import com.example.locallife.behavior.UserBehavior;
import com.example.locallife.behavior.UserBehaviorRepository;
import com.example.locallife.shop.Shop;
import com.example.locallife.shop.ShopRepository;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Function;
import java.util.stream.Collectors;

@Service
public class ShopRecommendationService {

    private static final int DEFAULT_RECOMMENDATION_LIMIT = 10;
    private static final int MAX_RECOMMENDATION_LIMIT = 50;
    private static final int HISTORY_LIMIT = 50;
    private static final int SIGNAL_LIMIT = 10_000;
    private static final int ITEMCF_NEIGHBOR_LIMIT = 50;
    private static final int MIN_CANDIDATE_POOL_SIZE = 20;
    private static final int CANDIDATE_POOL_MULTIPLIER = 5;
    private static final int MAX_TRIGGER_SHOP_COUNT = 5;
    private static final double POPULAR_WEIGHT = 0.25;
    private static final double ITEMCF_WEIGHT = 0.25;
    private static final double TYPE_FALLBACK_WEIGHT = 0.01;
    private static final double EARTH_RADIUS_METERS = 6_371_000.0;

    private final UserBehaviorRepository userBehaviorRepository;
    private final ShopRepository shopRepository;

    public ShopRecommendationService(
            UserBehaviorRepository userBehaviorRepository,
            ShopRepository shopRepository
    ) {
        this.userBehaviorRepository = userBehaviorRepository;
        this.shopRepository = shopRepository;
    }

    public List<RecommendedShopResponse> recommendShops(
            String userId,
            Integer limit,
            Double longitude,
            Double latitude,
            Double radiusMeters
    ) {
        int effectiveLimit = normalizeLimit(limit);
        boolean hasLocation = longitude != null && latitude != null;

        List<Shop> shops = shopRepository.findAll();
        if (shops.isEmpty()) {
            return List.of();
        }

        Map<Long, Shop> shopsById = shops.stream()
                .collect(Collectors.toMap(Shop::id, Function.identity()));
        List<UserBehavior> userBehaviors = userBehaviorRepository.findByUserId(userId.trim(), HISTORY_LIMIT);
        List<UserBehavior> signalBehaviors = userBehaviorRepository.findRecent(SIGNAL_LIMIT);
        Set<Long> seenShopIds = userBehaviors.stream()
                .map(UserBehavior::shopId)
                .collect(Collectors.toSet());

        int targetPoolSize = targetPoolSize(effectiveLimit, shops.size());
        Map<Long, CandidateScore> candidateScores = new HashMap<>();

        Map<Long, List<ItemSimilarity>> itemSimilarityTable = buildItemSimilarityTable(signalBehaviors, shopsById);
        addItemcfCandidates(candidateScores, userBehaviors, itemSimilarityTable, seenShopIds, shopsById);

        Map<Long, Double> popularScores = normalizeScores(buildPopularScores(signalBehaviors, shopsById));
        addPopularCandidates(candidateScores, popularScores, seenShopIds, shopsById, targetPoolSize);

        if (candidateScores.size() < targetPoolSize && !userBehaviors.isEmpty()) {
            addTypeFallbackCandidates(candidateScores, userBehaviors, seenShopIds, shopsById, targetPoolSize);
        }

        List<RecommendedShopResponse> candidates = new ArrayList<>();
        for (Map.Entry<Long, CandidateScore> entry : candidateScores.entrySet()) {
            Shop shop = shopsById.get(entry.getKey());
            if (shop == null) {
                continue;
            }

            Double distanceMeters = hasLocation
                    ? calculateDistanceMeters(longitude, latitude, shop.longitude(), shop.latitude())
                    : null;
            if (radiusMeters != null && radiusMeters > 0 && distanceMeters != null && distanceMeters > radiusMeters) {
                continue;
            }

            candidates.add(toResponse(shop, entry.getValue(), distanceMeters, shopsById));
        }

        return candidates.stream()
                .sorted(recommendationComparator(hasLocation))
                .limit(effectiveLimit)
                .toList();
    }

    private void addItemcfCandidates(
            Map<Long, CandidateScore> candidateScores,
            List<UserBehavior> userBehaviors,
            Map<Long, List<ItemSimilarity>> itemSimilarityTable,
            Set<Long> seenShopIds,
            Map<Long, Shop> shopsById
    ) {
        Map<Long, CandidateScore> rawScores = new HashMap<>();
        for (Long sourceShopId : positiveUniqueShopIds(userBehaviors, shopsById)) {
            for (ItemSimilarity similarity : itemSimilarityTable.getOrDefault(sourceShopId, List.of())) {
                Long candidateShopId = similarity.shopId();
                if (seenShopIds.contains(candidateShopId) || !shopsById.containsKey(candidateShopId)) {
                    continue;
                }
                CandidateScore score = rawScores.computeIfAbsent(candidateShopId, ignored -> new CandidateScore());
                score.itemcfScore += similarity.similarity();
                score.triggerShopIds.add(sourceShopId);
            }
        }

        Map<Long, Double> normalizedScores = normalizeScores(rawScores.entrySet().stream()
                .collect(Collectors.toMap(Map.Entry::getKey, entry -> entry.getValue().itemcfScore)));
        normalizedScores.entrySet().stream()
                .sorted(Map.Entry.<Long, Double>comparingByValue().reversed().thenComparing(Map.Entry.comparingByKey()))
                .forEach(entry -> {
                    CandidateScore rawScore = rawScores.get(entry.getKey());
                    CandidateScore targetScore = candidateScores.computeIfAbsent(entry.getKey(), ignored -> new CandidateScore());
                    targetScore.itemcfScore = Math.max(targetScore.itemcfScore, entry.getValue());
                    targetScore.triggerShopIds.addAll(rawScore.triggerShopIds);
                });
    }

    private void addPopularCandidates(
            Map<Long, CandidateScore> candidateScores,
            Map<Long, Double> popularScores,
            Set<Long> seenShopIds,
            Map<Long, Shop> shopsById,
            int targetPoolSize
    ) {
        popularScores.entrySet().stream()
                .filter(entry -> !seenShopIds.contains(entry.getKey()))
                .filter(entry -> shopsById.containsKey(entry.getKey()))
                .sorted(Map.Entry.<Long, Double>comparingByValue().reversed().thenComparing(Map.Entry.comparingByKey()))
                .limit(targetPoolSize)
                .forEach(entry -> {
                    CandidateScore score = candidateScores.computeIfAbsent(entry.getKey(), ignored -> new CandidateScore());
                    score.popularScore = Math.max(score.popularScore, entry.getValue());
                });
    }

    private void addTypeFallbackCandidates(
            Map<Long, CandidateScore> candidateScores,
            List<UserBehavior> userBehaviors,
            Set<Long> seenShopIds,
            Map<Long, Shop> shopsById,
            int targetPoolSize
    ) {
        Map<Long, LinkedHashSet<Long>> triggerShopIdsByType = buildTriggerShopIdsByType(userBehaviors, shopsById);
        Map<Long, Double> typeScores = normalizeScores(buildTypeScores(userBehaviors, shopsById));
        if (triggerShopIdsByType.isEmpty() || typeScores.isEmpty()) {
            return;
        }

        shopsById.values().stream()
                .filter(shop -> !seenShopIds.contains(shop.id()))
                .filter(shop -> typeScores.containsKey(shop.typeId()))
                .sorted(Comparator
                        .comparing((Shop shop) -> typeScores.get(shop.typeId())).reversed()
                        .thenComparing(Shop::id))
                .forEach(shop -> {
                    if (candidateScores.size() >= targetPoolSize) {
                        return;
                    }
                    CandidateScore score = candidateScores.computeIfAbsent(shop.id(), ignored -> new CandidateScore());
                    score.typeFallbackScore = Math.max(score.typeFallbackScore, typeScores.get(shop.typeId()));
                    score.triggerShopIds.addAll(triggerShopIdsByType.getOrDefault(shop.typeId(), new LinkedHashSet<>()));
                });
    }

    private Map<Long, Double> buildPopularScores(
            List<UserBehavior> behaviors,
            Map<Long, Shop> shopsById
    ) {
        return behaviors.stream()
                .filter(behavior -> shopsById.containsKey(behavior.shopId()))
                .collect(Collectors.groupingBy(
                        UserBehavior::shopId,
                        Collectors.summingDouble(this::positiveBehaviorWeight)
                ));
    }

    private Map<Long, List<ItemSimilarity>> buildItemSimilarityTable(
            List<UserBehavior> behaviors,
            Map<Long, Shop> shopsById
    ) {
        Map<String, List<UserBehavior>> behaviorsByUser = behaviors.stream()
                .filter(behavior -> shopsById.containsKey(behavior.shopId()))
                .collect(Collectors.groupingBy(UserBehavior::userId));
        Map<Long, Integer> shopPopularity = new HashMap<>();
        Map<Long, Map<Long, Integer>> cooccurrence = new HashMap<>();

        for (List<UserBehavior> userHistory : behaviorsByUser.values()) {
            List<Long> shopIds = positiveUniqueShopIds(userHistory, shopsById);
            for (Long shopId : shopIds) {
                shopPopularity.merge(shopId, 1, Integer::sum);
            }
            for (int leftIndex = 0; leftIndex < shopIds.size(); leftIndex++) {
                Long leftShopId = shopIds.get(leftIndex);
                for (int rightIndex = leftIndex + 1; rightIndex < shopIds.size(); rightIndex++) {
                    Long rightShopId = shopIds.get(rightIndex);
                    cooccurrence.computeIfAbsent(leftShopId, ignored -> new HashMap<>())
                            .merge(rightShopId, 1, Integer::sum);
                    cooccurrence.computeIfAbsent(rightShopId, ignored -> new HashMap<>())
                            .merge(leftShopId, 1, Integer::sum);
                }
            }
        }

        Map<Long, List<ItemSimilarity>> similarityTable = new HashMap<>();
        for (Map.Entry<Long, Map<Long, Integer>> entry : cooccurrence.entrySet()) {
            Long shopId = entry.getKey();
            List<ItemSimilarity> similarities = entry.getValue().entrySet().stream()
                    .map(neighbor -> {
                        Long neighborShopId = neighbor.getKey();
                        int togetherCount = neighbor.getValue();
                        double denominator = Math.sqrt(
                                shopPopularity.getOrDefault(shopId, 0)
                                        * shopPopularity.getOrDefault(neighborShopId, 0)
                        );
                        double similarity = denominator == 0 ? 0.0 : togetherCount / denominator;
                        return new ItemSimilarity(neighborShopId, similarity, togetherCount);
                    })
                    .filter(item -> item.similarity() > 0)
                    .sorted(Comparator
                            .comparing(ItemSimilarity::similarity).reversed()
                            .thenComparing(Comparator.comparing(ItemSimilarity::cooccurrence).reversed())
                            .thenComparing(ItemSimilarity::shopId))
                    .limit(ITEMCF_NEIGHBOR_LIMIT)
                    .toList();
            similarityTable.put(shopId, similarities);
        }
        return similarityTable;
    }

    private List<Long> positiveUniqueShopIds(
            List<UserBehavior> behaviors,
            Map<Long, Shop> shopsById
    ) {
        return behaviors.stream()
                .filter(behavior -> shopsById.containsKey(behavior.shopId()))
                .filter(behavior -> positiveBehaviorWeight(behavior) > 0)
                .map(UserBehavior::shopId)
                .distinct()
                .sorted()
                .toList();
    }

    private Map<Long, LinkedHashSet<Long>> buildTriggerShopIdsByType(
            List<UserBehavior> behaviors,
            Map<Long, Shop> shopsById
    ) {
        return behaviors.stream()
                .filter(behavior -> positiveBehaviorWeight(behavior) > 0)
                .map(UserBehavior::shopId)
                .distinct()
                .map(shopsById::get)
                .filter(shop -> shop != null)
                .collect(Collectors.groupingBy(
                        Shop::typeId,
                        Collectors.mapping(
                                Shop::id,
                                Collectors.toCollection(LinkedHashSet::new)
                        )
                ));
    }

    private Map<Long, Double> buildTypeScores(
            List<UserBehavior> behaviors,
            Map<Long, Shop> shopsById
    ) {
        return behaviors.stream()
                .filter(behavior -> shopsById.containsKey(behavior.shopId()))
                .filter(behavior -> positiveBehaviorWeight(behavior) > 0)
                .collect(Collectors.groupingBy(
                        behavior -> shopsById.get(behavior.shopId()).typeId(),
                        Collectors.summingDouble(this::positiveBehaviorWeight)
                ));
    }

    private Map<Long, Double> normalizeScores(Map<Long, Double> scores) {
        double maxScore = scores.values().stream()
                .filter(score -> score > 0)
                .mapToDouble(Double::doubleValue)
                .max()
                .orElse(0.0);
        if (maxScore <= 0) {
            return Map.of();
        }
        return scores.entrySet().stream()
                .filter(entry -> entry.getValue() > 0)
                .collect(Collectors.toMap(
                        Map.Entry::getKey,
                        entry -> entry.getValue() / maxScore
                ));
    }

    private double positiveBehaviorWeight(UserBehavior behavior) {
        String behaviorType = behavior.behaviorType().trim().toLowerCase();
        return switch (behaviorType) {
            case "order" -> 5.0;
            case "rating" -> behavior.score() != null && behavior.score() >= 4.0
                    ? behavior.score()
                    : 0.0;
            case "favorite" -> 3.0;
            case "click" -> 1.5;
            case "view" -> 1.0;
            default -> 1.0;
        };
    }

    private RecommendedShopResponse toResponse(
            Shop shop,
            CandidateScore score,
            Double distanceMeters,
            Map<Long, Shop> shopsById
    ) {
        List<Long> limitedTriggerShopIds = score.triggerShopIds.stream()
                .limit(MAX_TRIGGER_SHOP_COUNT)
                .toList();
        List<String> triggerShopNames = limitedTriggerShopIds.stream()
                .map(shopsById::get)
                .filter(triggerShop -> triggerShop != null)
                .map(Shop::name)
                .toList();
        return new RecommendedShopResponse(
                shop.id(),
                shop.name(),
                shop.typeId(),
                shop.avgPrice(),
                shop.address(),
                shop.longitude(),
                shop.latitude(),
                score.reason(),
                limitedTriggerShopIds,
                triggerShopNames,
                score.finalScore(),
                distanceMeters,
                shop.coordinateSystem(),
                shop.district(),
                shop.anchorPlaceId(),
                shop.anchorPlaceName(),
                shop.dataNature()
        );
    }

    private Comparator<RecommendedShopResponse> recommendationComparator(boolean hasLocation) {
        return (left, right) -> {
            int byScore = Double.compare(right.score(), left.score());
            if (byScore != 0) {
                return byScore;
            }
            if (hasLocation) {
                int byDistance = compareNullableDistance(left.distanceMeters(), right.distanceMeters());
                if (byDistance != 0) {
                    return byDistance;
                }
            }
            return Long.compare(left.shopId(), right.shopId());
        };
    }

    private int compareNullableDistance(Double left, Double right) {
        if (left == null && right == null) {
            return 0;
        }
        if (left == null) {
            return 1;
        }
        if (right == null) {
            return -1;
        }
        return Double.compare(left, right);
    }

    private int normalizeLimit(Integer limit) {
        if (limit == null || limit <= 0) {
            return DEFAULT_RECOMMENDATION_LIMIT;
        }
        return Math.min(limit, MAX_RECOMMENDATION_LIMIT);
    }

    private int targetPoolSize(int effectiveLimit, int shopCount) {
        int desiredPoolSize = Math.max(MIN_CANDIDATE_POOL_SIZE, effectiveLimit * CANDIDATE_POOL_MULTIPLIER);
        return Math.min(shopCount, desiredPoolSize);
    }

    private double calculateDistanceMeters(
            double userLongitude,
            double userLatitude,
            double shopLongitude,
            double shopLatitude
    ) {
        double userLatitudeRadians = Math.toRadians(userLatitude);
        double shopLatitudeRadians = Math.toRadians(shopLatitude);
        double latitudeDelta = Math.toRadians(shopLatitude - userLatitude);
        double longitudeDelta = Math.toRadians(shopLongitude - userLongitude);

        double haversine = Math.sin(latitudeDelta / 2) * Math.sin(latitudeDelta / 2)
                + Math.cos(userLatitudeRadians) * Math.cos(shopLatitudeRadians)
                * Math.sin(longitudeDelta / 2) * Math.sin(longitudeDelta / 2);
        double centralAngle = 2 * Math.atan2(Math.sqrt(haversine), Math.sqrt(1 - haversine));
        return EARTH_RADIUS_METERS * centralAngle;
    }

    private static final class CandidateScore {
        private double popularScore;
        private double itemcfScore;
        private double typeFallbackScore;
        private final LinkedHashSet<Long> triggerShopIds = new LinkedHashSet<>();

        private double finalScore() {
            return POPULAR_WEIGHT * popularScore
                    + ITEMCF_WEIGHT * itemcfScore
                    + TYPE_FALLBACK_WEIGHT * typeFallbackScore;
        }

        private String reason() {
            if (itemcfScore > 0 && popularScore > 0) {
                return "\u548c\u4f60\u5173\u6ce8\u8fc7\u7684\u5546\u6237\u76f8\u4f3c\uff0c\u540c\u65f6\u8fd1\u671f\u4e5f\u8f83\u70ed\u95e8";
            }
            if (itemcfScore > 0) {
                return "\u548c\u4f60\u5173\u6ce8\u8fc7\u7684\u5546\u6237\u76f8\u4f3c";
            }
            if (popularScore > 0) {
                return "\u8fd1\u671f\u70ed\u95e8\u5546\u6237";
            }
            return "\u4f60\u6700\u8fd1\u5173\u6ce8\u8fc7\u540c\u7c7b\u578b\u5546\u6237";
        }
    }

    private record ItemSimilarity(Long shopId, double similarity, int cooccurrence) {
    }
}
