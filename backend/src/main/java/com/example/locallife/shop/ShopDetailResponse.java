package com.example.locallife.shop;

// 详情专用响应：比列表用的 ShopResponse 多一个 phone（电话只在详情接口返回）
public record ShopDetailResponse(
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
        String dataNature
) {
    public ShopDetailResponse(
            Long id,
            String name,
            Long typeId,
            String address,
            Integer avgPrice,
            String phone
    ) {
        this(id, name, typeId, address, avgPrice, phone, null, null, null, null, null, null, null);
    }
}
