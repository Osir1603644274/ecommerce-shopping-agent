package com.example.locallife.recommendation;

import java.util.List;

public record RecommendedShopResponse(
        Long shopId,
        String shopName,
        Long typeId,
        Integer avgPrice,
        String address,
        Double longitude,
        Double latitude,
        String reason,
        List<Long> triggerShopIds,
        List<String> triggerShopNames,
        Double score,
        Double distanceMeters,
        String coordinateSystem,
        String district,
        String anchorPlaceId,
        String anchorPlaceName,
        String dataNature
) {
    public RecommendedShopResponse(
            Long shopId,
            String shopName,
            Long typeId,
            Integer avgPrice,
            String address,
            Double longitude,
            Double latitude,
            String reason,
            List<Long> triggerShopIds,
            List<String> triggerShopNames,
            Double score,
            Double distanceMeters
    ) {
        this(
                shopId, shopName, typeId, avgPrice, address, longitude, latitude,
                reason, triggerShopIds, triggerShopNames, score, distanceMeters,
                null, null, null, null, null
        );
    }
}
