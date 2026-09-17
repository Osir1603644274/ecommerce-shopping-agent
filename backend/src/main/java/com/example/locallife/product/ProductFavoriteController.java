package com.example.locallife.product;

import com.example.locallife.common.ApiResponse;
import com.example.locallife.common.ResourceNotFoundException;
import jakarta.validation.constraints.Positive;
import org.springframework.security.core.Authentication;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;
import java.util.List;
import java.util.Map;

@RestController
@Validated
@RequestMapping("/api/product-favorites")
public class ProductFavoriteController {
    private final ProductFavoriteMapper favorites;
    private final ProductRepository products;

    public ProductFavoriteController(ProductFavoriteMapper favorites, ProductRepository products) {
        this.favorites = favorites;
        this.products = products;
    }

    @GetMapping
    public ApiResponse<List<Product>> list(Authentication authentication) {
        return ApiResponse.ok(favorites.list(authentication.getName()));
    }

    @PutMapping("/{productId}")
    public ApiResponse<Map<String, Boolean>> add(@PathVariable @Positive long productId,
                                                Authentication authentication) {
        products.findById(productId).orElseThrow(() -> new ResourceNotFoundException("商品不存在"));
        favorites.add(authentication.getName(), productId);
        return ApiResponse.ok(Map.of("saved", true));
    }

    @DeleteMapping("/{productId}")
    public ApiResponse<Map<String, Boolean>> remove(@PathVariable @Positive long productId,
                                                   Authentication authentication) {
        favorites.remove(authentication.getName(), productId);
        return ApiResponse.ok(Map.of("saved", false));
    }
}
