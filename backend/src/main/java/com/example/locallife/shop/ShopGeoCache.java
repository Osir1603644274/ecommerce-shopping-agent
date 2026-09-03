package com.example.locallife.shop;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.geo.Circle;
import org.springframework.data.geo.Distance;
import org.springframework.data.geo.GeoResult;
import org.springframework.data.geo.GeoResults;
import org.springframework.data.geo.Metrics;
import org.springframework.data.geo.Point;
import org.springframework.data.redis.connection.RedisGeoCommands;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Objects;

@Component
@ConditionalOnProperty(prefix = "local-life.cache.shop", name = "enabled", havingValue = "true", matchIfMissing = true)
public class ShopGeoCache {

    static final String SHOP_GEO_KEY_PREFIX = "local-life:shop:geo:type:";

    private static final Logger log = LoggerFactory.getLogger(ShopGeoCache.class);

    private final StringRedisTemplate redisTemplate;

    public ShopGeoCache(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    public void rebuildTypeIndex(Long typeId, List<Shop> shops) {
        String key = geoKey(typeId);
        try {
            redisTemplate.delete(key);
            for (Shop shop : shops) {
                redisTemplate.opsForGeo().add(
                        key,
                        new Point(shop.longitude(), shop.latitude()),
                        shop.id().toString()
                );
            }
        } catch (RuntimeException ex) {
            log.warn("Failed to rebuild shop geo cache, key={}", key, ex);
        }
    }

    public List<NearbyShopCandidate> findNearby(
            Long typeId,
            Double longitude,
            Double latitude,
            Double radiusMeters,
            Integer limit
    ) {
        String key = geoKey(typeId);
        try {
            GeoResults<RedisGeoCommands.GeoLocation<String>> results = redisTemplate.opsForGeo().radius(
                    key,
                    new Circle(new Point(longitude, latitude), new Distance(radiusMeters / 1000.0, Metrics.KILOMETERS)),
                    RedisGeoCommands.GeoRadiusCommandArgs.newGeoRadiusArgs()
                            .includeDistance()
                            .sortAscending()
                            .limit(limit)
            );

            if (results == null) {
                return List.of();
            }

            return results.getContent().stream()
                    .map(this::toCandidate)
                    .filter(Objects::nonNull)
                    .toList();
        } catch (RuntimeException ex) {
            log.warn("Failed to query shop geo cache, key={}", key, ex);
            return List.of();
        }
    }

    static String geoKey(Long typeId) {
        return SHOP_GEO_KEY_PREFIX + typeId;
    }

    private NearbyShopCandidate toCandidate(GeoResult<RedisGeoCommands.GeoLocation<String>> result) {
        try {
            Long shopId = Long.valueOf(result.getContent().getName());
            Double distanceMeters = result.getDistance().getValue() * 1000.0;
            return new NearbyShopCandidate(shopId, distanceMeters);
        } catch (RuntimeException ex) {
            log.warn("Failed to parse shop geo result, member={}", result.getContent().getName(), ex);
            return null;
        }
    }
}