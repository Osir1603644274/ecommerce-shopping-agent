package com.example.locallife.shop;

public record ShopResponse(
        Long id,
        String name,
        Long typeId,
        String address,
        Integer avgPrice,
        Double longitude,
        Double latitude,
        String coordinateSystem,
        String district,
        String anchorPlaceId,
        String anchorPlaceName,
        String dataNature
) {
    public ShopResponse(Long id, String name, Long typeId, String address, Integer avgPrice) {
        this(id, name, typeId, address, avgPrice, null, null, null, null, null, null, null);
    }
}
