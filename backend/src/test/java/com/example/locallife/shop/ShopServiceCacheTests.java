package com.example.locallife.shop;

import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

class ShopServiceCacheTests {

    private final ShopRepository shopRepository = mock(ShopRepository.class);
    private final ShopCache shopCache = mock(ShopCache.class);
    private final ShopGeoCache shopGeoCache = mock(ShopGeoCache.class);
    private final ShopService shopService = new ShopService(
            shopRepository,
            Optional.of(shopCache),
            Optional.of(shopGeoCache)
    );

    @Test
    void getShopReturnsCachedDetailWithoutQueryingDatabase() {
        ShopDetailResponse cachedShop = new ShopDetailResponse(
                3L, "清晨手冲咖啡", 2L, "文化广场 3 号", 35, "010-8888-0003");
        when(shopCache.getDetail(3L)).thenReturn(ShopCacheLookup.hit(cachedShop));

        Optional<ShopDetailResponse> result = shopService.getShop(3L);

        assertThat(result).contains(cachedShop);
        verify(shopCache).getDetail(3L);
        verifyNoInteractions(shopRepository);
        verify(shopCache, never()).tryLockDetail(3L);
        verify(shopCache, never()).putDetail(3L, cachedShop);
    }

    @Test
    void getShopQueriesDatabaseAndWritesCacheWhenCacheMissesAndLockIsAcquired() {
        Shop shop = sampleShop(3L, 2L, "清晨手冲咖啡", "文化广场 3 号", 35, "010-8888-0003", 116.4000, 39.9000);
        ShopDetailResponse expected = new ShopDetailResponse(
                3L, "清晨手冲咖啡", 2L, "文化广场 3 号", 35, "010-8888-0003",
                116.4000, 39.9000, "BD-09", null, null, null, "synthetic_seed");
        when(shopCache.getDetail(3L)).thenReturn(ShopCacheLookup.miss(), ShopCacheLookup.miss());
        when(shopCache.tryLockDetail(3L)).thenReturn(Optional.of("lock-3"));
        when(shopRepository.findById(3L)).thenReturn(Optional.of(shop));

        Optional<ShopDetailResponse> result = shopService.getShop(3L);

        assertThat(result).contains(expected);
        verify(shopCache, times(2)).getDetail(3L);
        verify(shopCache).tryLockDetail(3L);
        verify(shopRepository).findById(3L);
        verify(shopCache).putDetail(3L, expected);
        verify(shopCache).unlockDetail(3L, "lock-3");
    }

    @Test
    void getShopWritesEmptyCacheWhenShopDoesNotExistAndLockIsAcquired() {
        when(shopCache.getDetail(999L)).thenReturn(ShopCacheLookup.miss(), ShopCacheLookup.miss());
        when(shopCache.tryLockDetail(999L)).thenReturn(Optional.of("lock-999"));
        when(shopRepository.findById(999L)).thenReturn(Optional.empty());

        Optional<ShopDetailResponse> result = shopService.getShop(999L);

        assertThat(result).isEmpty();
        verify(shopCache, times(2)).getDetail(999L);
        verify(shopCache).tryLockDetail(999L);
        verify(shopRepository).findById(999L);
        verify(shopCache, never()).putDetail(eq(999L), any());
        verify(shopCache).putEmptyDetail(999L);
        verify(shopCache).unlockDetail(999L, "lock-999");
    }

    @Test
    void getShopReturnsEmptyWithoutQueryingDatabaseWhenEmptyCacheHits() {
        when(shopCache.getDetail(999L)).thenReturn(ShopCacheLookup.empty());

        Optional<ShopDetailResponse> result = shopService.getShop(999L);

        assertThat(result).isEmpty();
        verify(shopCache).getDetail(999L);
        verifyNoInteractions(shopRepository);
        verify(shopCache, never()).tryLockDetail(999L);
        verify(shopCache, never()).putDetail(eq(999L), any());
        verify(shopCache, never()).putEmptyDetail(999L);
    }

    @Test
    void getShopWaitsAndReturnsCacheWhenRebuildLockIsHeldByAnotherRequest() {
        ShopDetailResponse cachedShop = new ShopDetailResponse(
                3L, "清晨手冲咖啡", 2L, "文化广场 3 号", 35, "010-8888-0003");
        when(shopCache.getDetail(3L)).thenReturn(ShopCacheLookup.miss(), ShopCacheLookup.hit(cachedShop));
        when(shopCache.tryLockDetail(3L)).thenReturn(Optional.empty());

        Optional<ShopDetailResponse> result = shopService.getShop(3L);

        assertThat(result).contains(cachedShop);
        verify(shopCache, times(2)).getDetail(3L);
        verify(shopCache).tryLockDetail(3L);
        verifyNoInteractions(shopRepository);
        verify(shopCache, never()).putDetail(eq(3L), any());
        verify(shopCache, never()).unlockDetail(eq(3L), anyString());
    }

    @Test
    void updateShopDeletesCachedDetailAfterDatabaseUpdate() {
        UpdateShopRequest request = new UpdateShopRequest("文化广场 3 号北门", 39, "010-8888-9999");
        Shop updatedShop = sampleShop(3L, 2L, "清晨手冲咖啡", "文化广场 3 号北门", 39, "010-8888-9999", 116.4000, 39.9000);
        ShopDetailResponse expected = new ShopDetailResponse(
                3L, "清晨手冲咖啡", 2L, "文化广场 3 号北门", 39, "010-8888-9999",
                116.4000, 39.9000, "BD-09", null, null, null, "synthetic_seed");
        when(shopRepository.updateDetails(3L, request)).thenReturn(1);
        when(shopRepository.findById(3L)).thenReturn(Optional.of(updatedShop));

        ShopDetailResponse result = shopService.updateShop(3L, request);

        assertThat(result).isEqualTo(expected);
        verify(shopRepository).updateDetails(3L, request);
        verify(shopCache).deleteDetail(3L);
        verify(shopRepository).findById(3L);
    }

    @Test
    void updateShopDoesNotDeleteCacheWhenShopDoesNotExist() {
        UpdateShopRequest request = new UpdateShopRequest("不存在地址", 39, "010-8888-9999");
        when(shopRepository.updateDetails(999L, request)).thenReturn(0);

        assertThatThrownBy(() -> shopService.updateShop(999L, request))
                .hasMessage("商户不存在");

        verify(shopRepository).updateDetails(999L, request);
        verify(shopCache, never()).deleteDetail(999L);
    }

    @Test
    void listNearbyShopsUsesRedisGeoCandidatesWhenAvailable() {
        List<Shop> coffeeShops = List.of(
                sampleShop(3L, 2L, "清晨手冲咖啡", "文化广场 3 号", 35, "010-8888-0003", 116.4000, 39.9000),
                sampleShop(7L, 2L, "纸间咖啡书屋", "学院路 18 号", 42, "010-8888-0007", 116.4020, 39.9010)
        );
        when(shopRepository.findByTypeId(2L)).thenReturn(coffeeShops);
        when(shopGeoCache.findNearby(2L, 116.4000, 39.9000, 500.0, 5))
                .thenReturn(List.of(
                        new NearbyShopCandidate(3L, 0.0),
                        new NearbyShopCandidate(7L, 203.0)
                ));

        List<NearbyShopResponse> result = shopService.listNearbyShops(2L, 116.4000, 39.9000, 500.0, 5);

        assertThat(result).extracting(NearbyShopResponse::id).containsExactly(3L, 7L);
        assertThat(result).extracting(NearbyShopResponse::distanceMeters).containsExactly(0.0, 203.0);
        verify(shopRepository).findByTypeId(2L);
        verify(shopGeoCache, never()).rebuildTypeIndex(eq(2L), any());
    }

    @Test
    void listNearbyShopsRebuildsGeoIndexWhenRedisGeoReturnsEmpty() {
        List<Shop> coffeeShops = List.of(
                sampleShop(3L, 2L, "清晨手冲咖啡", "文化广场 3 号", 35, "010-8888-0003", 116.4000, 39.9000)
        );
        when(shopRepository.findByTypeId(2L)).thenReturn(coffeeShops);
        when(shopGeoCache.findNearby(2L, 116.4000, 39.9000, 500.0, 5))
                .thenReturn(List.of(), List.of(new NearbyShopCandidate(3L, 0.0)));

        List<NearbyShopResponse> result = shopService.listNearbyShops(2L, 116.4000, 39.9000, 500.0, 5);

        assertThat(result).extracting(NearbyShopResponse::id).containsExactly(3L);
        verify(shopGeoCache).rebuildTypeIndex(2L, coffeeShops);
        verify(shopGeoCache, times(2)).findNearby(2L, 116.4000, 39.9000, 500.0, 5);
    }

    @Test
    void listNearbyShopsFallsBackToDatabaseDistanceWhenGeoCacheIsUnavailable() {
        ShopService serviceWithoutGeoCache = new ShopService(shopRepository, Optional.of(shopCache), Optional.empty());
        List<Shop> coffeeShops = List.of(
                sampleShop(3L, 2L, "清晨手冲咖啡", "文化广场 3 号", 35, "010-8888-0003", 116.4000, 39.9000),
                sampleShop(7L, 2L, "纸间咖啡书屋", "学院路 18 号", 42, "010-8888-0007", 116.4020, 39.9010),
                sampleShop(8L, 2L, "街角共创咖啡", "创业大街 66 号", 39, "010-8888-0008", 116.4150, 39.9020)
        );
        when(shopRepository.findByTypeId(2L)).thenReturn(coffeeShops);

        List<NearbyShopResponse> result = serviceWithoutGeoCache.listNearbyShops(2L, 116.4000, 39.9000, 500.0, 10);

        assertThat(result).extracting(NearbyShopResponse::id).containsExactly(3L, 7L);
        assertThat(result.get(0).distanceMeters()).isEqualTo(0.0);
        assertThat(result.get(1).distanceMeters()).isLessThan(250.0);
    }

    @Test
    void listNearbyShopsCanSearchAllTypesWithoutUsingTypeGeoIndex() {
        List<Shop> shops = List.of(
                sampleShop(3L, 2L, "清晨手冲咖啡", "文化广场 3 号", 35, "010-8888-0003", 116.4000, 39.9000),
                sampleShop(9L, 1L, "邻里小馆", "文化广场 9 号", 60, "010-8888-0009", 116.4010, 39.9005)
        );
        when(shopRepository.findAll()).thenReturn(shops);

        List<NearbyShopResponse> result = shopService.listNearbyShops(
                null, 116.4000, 39.9000, 500.0, 10
        );

        assertThat(result).extracting(NearbyShopResponse::id).containsExactly(3L, 9L);
        verify(shopRepository).findAll();
        verifyNoInteractions(shopGeoCache);
    }

    private Shop sampleShop(
            Long id,
            Long typeId,
            String name,
            String address,
            Integer avgPrice,
            String phone,
            Double longitude,
            Double latitude
    ) {
        return new Shop(
                id,
                name,
                typeId,
                address,
                avgPrice,
                phone,
                longitude,
                latitude,
                LocalDateTime.parse("2026-06-01T10:00:00"),
                LocalDateTime.parse("2026-06-01T10:00:00")
        );
    }
}
