package com.example.locallife.shop;

import com.example.locallife.common.ApiResponse;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Optional;

@RestController
@RequestMapping("/api/shops")
public class ShopController {

    private final ShopService shopService;

    public ShopController(ShopService shopService) {
        this.shopService = shopService;
    }

    @GetMapping
    public ApiResponse<List<ShopResponse>> listShops(
            @RequestParam(required = false) Long typeId,
            @RequestParam(required = false) String name
    ) {
        return ApiResponse.ok(shopService.listShops(typeId, name));
    }

    @GetMapping("/nearby")
    public ApiResponse<List<NearbyShopResponse>> listNearbyShops(
            @RequestParam(required = false) Long typeId,
            @RequestParam Double longitude,
            @RequestParam Double latitude,
            @RequestParam(required = false) Double radiusMeters,
            @RequestParam(required = false) Integer limit
    ) {
        return ApiResponse.ok(shopService.listNearbyShops(typeId, longitude, latitude, radiusMeters, limit));
    }

    @GetMapping("/{id}")
    public ResponseEntity<ApiResponse<ShopDetailResponse>> getShop(@PathVariable Long id) {
        Optional<ShopDetailResponse> shop = shopService.getShop(id);
        if (shop.isEmpty()) {
            return ResponseEntity
                    .status(HttpStatus.NOT_FOUND)
                    .body(ApiResponse.fail("商户不存在"));
        }

        return ResponseEntity.ok(ApiResponse.ok(shop.get()));
    }

    @PutMapping("/{id}")
    public ApiResponse<ShopDetailResponse> updateShop(
            @PathVariable Long id,
            @Valid @RequestBody UpdateShopRequest request
    ) {
        return ApiResponse.ok(shopService.updateShop(id, request));
    }
}
