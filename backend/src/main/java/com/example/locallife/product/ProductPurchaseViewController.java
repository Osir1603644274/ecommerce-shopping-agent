package com.example.locallife.product;

import com.example.locallife.common.ApiResponse;
import com.example.locallife.common.ResourceNotFoundException;
import org.springframework.web.bind.annotation.*;

/** Read aggregation only: cached descriptive data, current purchase conditions. */
@RestController
public class ProductPurchaseViewController {
    private final ProductService products;
    private final ProductOfferController offers;

    public ProductPurchaseViewController(ProductService products, ProductOfferController offers) {
        this.products = products;
        this.offers = offers;
    }

    @GetMapping("/api/products/{id}/purchase-view")
    public ApiResponse<View> get(@PathVariable long id) {
        var product = products.get(id).orElseThrow(() -> new ResourceNotFoundException("商品不存在"));
        // Do not derive purchase eligibility from possibly stale detail-cache fields.
        return ApiResponse.ok(new View(product, offers.get(id).data()));
    }

    public record View(ProductDetailResponse product, ProductOfferController.View offer) { }
}
