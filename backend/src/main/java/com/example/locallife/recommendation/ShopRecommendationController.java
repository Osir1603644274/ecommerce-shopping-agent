package com.example.locallife.recommendation;

import com.example.locallife.common.ApiResponse;
import jakarta.validation.constraints.NotBlank;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@Validated
@RestController
@RequestMapping("/api/recommendations")
public class ShopRecommendationController {

    private final ShopRecommendationService shopRecommendationService;

    public ShopRecommendationController(ShopRecommendationService shopRecommendationService) {
        this.shopRecommendationService = shopRecommendationService;
    }

    @GetMapping("/shops")
    public ApiResponse<List<RecommendedShopResponse>> recommendShops(
            @RequestParam @NotBlank(message = "用户编号不能为空") String userId,
            @RequestParam(required = false) Integer limit,
            @RequestParam(required = false) Double longitude,
            @RequestParam(required = false) Double latitude,
            @RequestParam(required = false) Double radiusMeters
    ) {
        return ApiResponse.ok(shopRecommendationService.recommendShops(
                userId,
                limit,
                longitude,
                latitude,
                radiusMeters
        ));
    }
}