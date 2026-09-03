package com.example.locallife.shop;

import java.time.LocalDateTime;

public record Shop(
        Long id,
        String name,
        Long typeId,
        String address,
        Integer avgPrice,
        String phone,
        Double longitude,
        Double latitude,
        String coordinateSystem,
        String district,
        String anchorPlaceId,
        String anchorPlaceName,
        String dataNature,
        String source,
        String sourceEntityId,
        String originalName,
        String originalAddress,
        Double originalLongitude,
        Double originalLatitude,
        String localizationVersion,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
    public Shop(
            Long id,
            String name,
            Long typeId,
            String address,
            Integer avgPrice,
            String phone,
            Double longitude,
            Double latitude,
            LocalDateTime createdAt,
            LocalDateTime updatedAt
    ) {
        this(
                id, name, typeId, address, avgPrice, phone, longitude, latitude,
                "BD-09", null, null, null, "synthetic_seed", "seed", null,
                null, null, null, null, null, createdAt, updatedAt
        );
    }
}
