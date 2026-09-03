package com.example.locallife.shop;

public record NearbyShopResponse(
        Long id,
        String name,
        Long typeId,
        String address,
        Integer avgPrice,
        Double longitude,
        Double latitude,
        Double distanceMeters,
        String coordinateSystem,
        String district,
        String anchorPlaceId,
        String anchorPlaceName,
        String dataNature
) {
    public NearbyShopResponse(
            Long id,
            String name,
            Long typeId,
            String address,
            Integer avgPrice,
            Double longitude,
            Double latitude,
            Double distanceMeters
    ) {
        this(
                id, name, typeId, address, avgPrice, longitude, latitude, distanceMeters,
                null, null, null, null, null
        );
    }
}
