package com.example.locallife.shop;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.data.geo.Circle;
import org.springframework.data.geo.Distance;
import org.springframework.data.geo.GeoResult;
import org.springframework.data.geo.GeoResults;
import org.springframework.data.geo.Metrics;
import org.springframework.data.geo.Point;
import org.springframework.data.redis.connection.RedisGeoCommands;
import org.springframework.data.redis.core.GeoOperations;
import org.springframework.data.redis.core.StringRedisTemplate;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class ShopGeoCacheTests {

    private final StringRedisTemplate redisTemplate = mock(StringRedisTemplate.class);
    private final GeoOperations<String, String> geoOperations = mock(GeoOperations.class);
    private ShopGeoCache shopGeoCache;

    @BeforeEach
    void setUp() {
        when(redisTemplate.opsForGeo()).thenReturn(geoOperations);
        shopGeoCache = new ShopGeoCache(redisTemplate);
    }

    @Test
    void rebuildTypeIndexDeletesOldKeyAndAddsShopLocations() {
        List<Shop> shops = List.of(
                sampleShop(3L, 2L, 116.4000, 39.9000),
                sampleShop(7L, 2L, 116.4020, 39.9010)
        );

        shopGeoCache.rebuildTypeIndex(2L, shops);

        String key = ShopGeoCache.geoKey(2L);
        verify(redisTemplate).delete(key);
        verify(geoOperations).add(key, new Point(116.4000, 39.9000), "3");
        verify(geoOperations).add(key, new Point(116.4020, 39.9010), "7");
    }

    @Test
    void findNearbyReturnsShopIdsAndDistancesFromRedisGeo() {
        String key = ShopGeoCache.geoKey(2L);
        GeoResults<RedisGeoCommands.GeoLocation<String>> geoResults = new GeoResults<>(List.of(
                new GeoResult<>(
                        new RedisGeoCommands.GeoLocation<>("3", new Point(116.4000, 39.9000)),
                        new Distance(0.0, Metrics.KILOMETERS)
                ),
                new GeoResult<>(
                        new RedisGeoCommands.GeoLocation<>("7", new Point(116.4020, 39.9010)),
                        new Distance(0.203, Metrics.KILOMETERS)
                )
        ));
        when(geoOperations.radius(
                eq(key),
                any(Circle.class),
                any(RedisGeoCommands.GeoRadiusCommandArgs.class)
        )).thenReturn(geoResults);

        List<NearbyShopCandidate> result = shopGeoCache.findNearby(2L, 116.4000, 39.9000, 500.0, 5);

        assertThat(result).containsExactly(
                new NearbyShopCandidate(3L, 0.0),
                new NearbyShopCandidate(7L, 203.0)
        );
    }

    private Shop sampleShop(Long id, Long typeId, Double longitude, Double latitude) {
        return new Shop(
                id,
                "shop-" + id,
                typeId,
                "address-" + id,
                35,
                "010-8888-000" + id,
                longitude,
                latitude,
                LocalDateTime.parse("2026-06-01T10:00:00"),
                LocalDateTime.parse("2026-06-01T10:00:00")
        );
    }
}