package com.example.locallife.shop;

import com.example.locallife.common.ApiResponse;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Optional;

@RestController
@RequestMapping("/api/shop-types")
public class ShopTypeController {

    private final ShopTypeService shopTypeService;

    public ShopTypeController(ShopTypeService shopTypeService) {
        this.shopTypeService = shopTypeService;
    }

    @GetMapping
    public ApiResponse<List<ShopTypeResponse>> listShopTypes(
            @RequestParam(required = false) String keyword
    ) {
        return ApiResponse.ok(shopTypeService.listShopTypes(keyword));
    }

    @GetMapping("/{id}")
    public ResponseEntity<ApiResponse<ShopTypeResponse>> getShopType(@PathVariable Long id) {
        Optional<ShopTypeResponse> shopType = shopTypeService.getShopType(id);
        if (shopType.isEmpty()) {
            return ResponseEntity
                    .status(HttpStatus.NOT_FOUND)
                    .body(ApiResponse.fail("商户分类不存在"));
        }

        return ResponseEntity.ok(ApiResponse.ok(shopType.get()));
    }
}
