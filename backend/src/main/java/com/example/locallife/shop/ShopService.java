package com.example.locallife.shop;

import com.example.locallife.common.ResourceNotFoundException;
import com.example.locallife.integration.DomainEventTypes;
import com.example.locallife.integration.OutboxService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Function;
import java.util.stream.Collectors;

@Service
public class ShopService {

    private static final int CACHE_REBUILD_WAIT_ATTEMPTS = 3;
    private static final long CACHE_REBUILD_WAIT_MILLIS = 50L;
    private static final double DEFAULT_NEARBY_RADIUS_METERS = 2_000.0;
    private static final int DEFAULT_NEARBY_LIMIT = 10;
    private static final int MAX_NEARBY_LIMIT = 50;
    private static final double EARTH_RADIUS_METERS = 6_371_000.0;

    private final ShopRepository shopRepository;
    private final Optional<ShopCache> shopCache;
    private final Optional<ShopGeoCache> shopGeoCache;
    private final Optional<ShopSearchPort> shopSearch;
    private final Optional<OutboxService> outboxService;
    private final com.example.locallife.integration.CacheInvalidationRequests invalidation;

    public ShopService(
            ShopRepository shopRepository,
            Optional<ShopCache> shopCache,
            Optional<ShopGeoCache> shopGeoCache
    ) {
        this(shopRepository, shopCache, shopGeoCache, Optional.empty(), Optional.empty());
    }

    public ShopService(
            ShopRepository shopRepository,
            Optional<ShopCache> shopCache,
            Optional<ShopGeoCache> shopGeoCache,
            Optional<ShopSearchPort> shopSearch,
            Optional<OutboxService> outboxService
    ) {
        this(shopRepository,shopCache,shopGeoCache,shopSearch,outboxService,null);
    }

    @Autowired
    public ShopService(ShopRepository shopRepository,Optional<ShopCache> shopCache,
            Optional<ShopGeoCache> shopGeoCache,Optional<ShopSearchPort> shopSearch,
            Optional<OutboxService> outboxService,com.example.locallife.integration.CacheInvalidationRequests invalidation) {
        this.shopRepository = shopRepository;
        this.shopCache = shopCache;
        this.shopGeoCache = shopGeoCache;
        this.shopSearch = shopSearch;
        this.outboxService = outboxService;
        this.invalidation = invalidation;
    }

    public List<ShopResponse> listShops(Long typeId, String name) {
        String normalizedName = name == null || name.isBlank() ? null : name.strip();
        if (normalizedName != null && shopSearch.isPresent()) {
            Optional<List<Long>> searchIds = shopSearch.get().search(typeId, normalizedName, 500);
            if (searchIds.isPresent()) {
                return searchIds.get().stream()
                        .map(shopRepository::findById)
                        .flatMap(Optional::stream)
                        .filter(shop -> typeId == null || typeId.equals(shop.typeId()))
                        .map(this::toResponse)
                        .toList();
            }
        }
        return shopRepository.findByFilters(typeId, normalizedName).stream()
                .map(this::toResponse)
                .toList();
    }

    public List<NearbyShopResponse> listNearbyShops(
            Long typeId,
            Double longitude,
            Double latitude,
            Double radiusMeters,
            Integer limit
    ) {
        double effectiveRadiusMeters = normalizeRadiusMeters(radiusMeters);
        int effectiveLimit = normalizeLimit(limit);
        List<Shop> shops = typeId == null ? shopRepository.findAll() : shopRepository.findByTypeId(typeId);
        if (shops.isEmpty()) {
            return List.of();
        }

        if (typeId != null && shopGeoCache.isPresent()) {
            ShopGeoCache geoCache = shopGeoCache.get();
            List<NearbyShopCandidate> candidates = geoCache.findNearby(
                    typeId,
                    longitude,
                    latitude,
                    effectiveRadiusMeters,
                    effectiveLimit
            );
            if (candidates.isEmpty()) {
                geoCache.rebuildTypeIndex(typeId, shops);
                candidates = geoCache.findNearby(typeId, longitude, latitude, effectiveRadiusMeters, effectiveLimit);
            }
            if (!candidates.isEmpty()) {
                Map<Long, Shop> shopsById = shops.stream()
                        .collect(Collectors.toMap(Shop::id, Function.identity()));
                List<NearbyShopResponse> nearbyShops = candidates.stream()
                        .map(candidate -> toNearbyResponse(shopsById.get(candidate.shopId()), candidate.distanceMeters()))
                        .filter(Optional::isPresent)
                        .map(Optional::get)
                        .limit(effectiveLimit)
                        .toList();
                if (!nearbyShops.isEmpty()) {
                    return nearbyShops;
                }
            }
        }

        return shops.stream()
                .map(shop -> toNearbyResponse(
                        shop,
                        calculateDistanceMeters(longitude, latitude, shop.longitude(), shop.latitude())
                ))
                .filter(response -> response.distanceMeters() <= effectiveRadiusMeters)
                .sorted(Comparator.comparing(NearbyShopResponse::distanceMeters))
                .limit(effectiveLimit)
                .toList();
    }

    public Optional<ShopDetailResponse> getShop(Long id) {
        if (shopCache.isEmpty()) {
            return shopRepository.findById(id).map(this::toDetailResponse);
        }

        ShopCache cache = shopCache.get();
        ShopCacheLookup cachedShop = cache.getDetail(id);
        if (cachedShop.isHit()) {
            return Optional.of(cachedShop.detail());
        }
        if (cachedShop.isEmpty()) {
            return Optional.empty();
        }

        Optional<String> lockToken = cache.tryLockDetail(id);
        if (lockToken.isEmpty()) {
            return waitForCacheRebuild(id, cache);
        }

        try {
            ShopCacheLookup latestCachedShop = cache.getDetail(id);
            if (latestCachedShop.isHit()) {
                return Optional.of(latestCachedShop.detail());
            }
            if (latestCachedShop.isEmpty()) {
                return Optional.empty();
            }

            return queryShopAndWriteCache(id, cache);
        } finally {
            cache.unlockDetail(id, lockToken.get());
        }
    }

    @Transactional
    public ShopDetailResponse updateShop(Long id, UpdateShopRequest request) {
        int updatedRows = shopRepository.updateDetails(id, request);
        if (invalidation != null) invalidation.shopUpdated(id);
        if (updatedRows == 0) {
            throw new ResourceNotFoundException("商户不存在");
        }

        outboxService.ifPresent(outbox -> outbox.append(
                "SHOP",
                id.toString(),
                DomainEventTypes.SHOP_UPDATED_V1,
                Map.of("shopId", id)
        ));
        shopCache.ifPresent(cache -> {
            if (org.springframework.transaction.support.TransactionSynchronizationManager.isSynchronizationActive()) {
                org.springframework.transaction.support.TransactionSynchronizationManager.registerSynchronization(
                        new org.springframework.transaction.support.TransactionSynchronization() {
                            @Override
                            public void afterCommit() { cache.deleteDetail(id); }
                        });
            } else {
                cache.deleteDetail(id);
            }
        });
        return shopRepository.findById(id)
                .map(this::toDetailResponse)
                .orElseThrow(() -> new IllegalStateException("更新商户后无法查询商户"));
    }

    private Optional<ShopDetailResponse> waitForCacheRebuild(Long id, ShopCache cache) {
        for (int attempt = 0; attempt < CACHE_REBUILD_WAIT_ATTEMPTS; attempt++) {
            if (!sleepBeforeRetry()) {
                break;
            }

            ShopCacheLookup cachedShop = cache.getDetail(id);
            if (cachedShop.isHit()) {
                return Optional.of(cachedShop.detail());
            }
            if (cachedShop.isEmpty()) {
                return Optional.empty();
            }
        }

        return shopRepository.findById(id).map(this::toDetailResponse);
    }

    private Optional<ShopDetailResponse> queryShopAndWriteCache(Long id, ShopCache cache) {
        Optional<ShopDetailResponse> shop = shopRepository.findById(id)
                .map(this::toDetailResponse);
        if (shop.isPresent()) {
            cache.putDetail(id, shop.get());
        } else {
            cache.putEmptyDetail(id);
        }
        return shop;
    }

    private boolean sleepBeforeRetry() {
        try {
            Thread.sleep(CACHE_REBUILD_WAIT_MILLIS);
            return true;
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    private ShopResponse toResponse(Shop shop) {
        return new ShopResponse(
                shop.id(), shop.name(), shop.typeId(), shop.address(), shop.avgPrice(),
                shop.longitude(), shop.latitude(), shop.coordinateSystem(), shop.district(),
                shop.anchorPlaceId(), shop.anchorPlaceName(), shop.dataNature()
        );
    }

    private ShopDetailResponse toDetailResponse(Shop shop) {
        return new ShopDetailResponse(
                shop.id(), shop.name(), shop.typeId(), shop.address(), shop.avgPrice(), shop.phone(),
                shop.longitude(), shop.latitude(), shop.coordinateSystem(), shop.district(),
                shop.anchorPlaceId(), shop.anchorPlaceName(), shop.dataNature());
    }

    private Optional<NearbyShopResponse> toNearbyResponse(Shop shop, Double distanceMeters) {
        if (shop == null) {
            return Optional.empty();
        }
        return Optional.of(new NearbyShopResponse(
                shop.id(),
                shop.name(),
                shop.typeId(),
                shop.address(),
                shop.avgPrice(),
                shop.longitude(),
                shop.latitude(),
                distanceMeters,
                shop.coordinateSystem(),
                shop.district(),
                shop.anchorPlaceId(),
                shop.anchorPlaceName(),
                shop.dataNature()
        ));
    }

    private NearbyShopResponse toNearbyResponse(Shop shop, double distanceMeters) {
        return new NearbyShopResponse(
                shop.id(),
                shop.name(),
                shop.typeId(),
                shop.address(),
                shop.avgPrice(),
                shop.longitude(),
                shop.latitude(),
                distanceMeters,
                shop.coordinateSystem(),
                shop.district(),
                shop.anchorPlaceId(),
                shop.anchorPlaceName(),
                shop.dataNature()
        );
    }

    private double normalizeRadiusMeters(Double radiusMeters) {
        if (radiusMeters == null || radiusMeters <= 0) {
            return DEFAULT_NEARBY_RADIUS_METERS;
        }
        return radiusMeters;
    }

    private int normalizeLimit(Integer limit) {
        if (limit == null || limit <= 0) {
            return DEFAULT_NEARBY_LIMIT;
        }
        return Math.min(limit, MAX_NEARBY_LIMIT);
    }

    private double calculateDistanceMeters(
            Double startLongitude,
            Double startLatitude,
            Double targetLongitude,
            Double targetLatitude
    ) {
        double startLatitudeRadians = Math.toRadians(startLatitude);
        double targetLatitudeRadians = Math.toRadians(targetLatitude);
        double latitudeDeltaRadians = Math.toRadians(targetLatitude - startLatitude);
        double longitudeDeltaRadians = Math.toRadians(targetLongitude - startLongitude);

        double haversine = Math.sin(latitudeDeltaRadians / 2) * Math.sin(latitudeDeltaRadians / 2)
                + Math.cos(startLatitudeRadians) * Math.cos(targetLatitudeRadians)
                * Math.sin(longitudeDeltaRadians / 2) * Math.sin(longitudeDeltaRadians / 2);
        double centralAngle = 2 * Math.atan2(Math.sqrt(haversine), Math.sqrt(1 - haversine));
        return EARTH_RADIUS_METERS * centralAngle;
    }
}
