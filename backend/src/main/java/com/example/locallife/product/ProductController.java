package com.example.locallife.product;

import com.example.locallife.common.ApiResponse;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/products")
public class ProductController {
    private final ProductService service;

    public ProductController(ProductService service) {
        this.service = service;
    }

    @GetMapping
    public ApiResponse<List<ProductSummaryResponse>> list(
            @RequestParam(required = false) String query,
            @RequestParam(required = false) String category,
            @RequestParam(required = false) String brand,
            @RequestParam(required = false) Long minPriceMinor,
            @RequestParam(required = false) Long maxPriceMinor,
            @RequestParam(required = false) Integer limit,
            @RequestParam(required = false) String catalogVersion
    ) {
        return ApiResponse.ok(service.searchWithTrace(query, category, brand, minPriceMinor, maxPriceMinor, limit,catalogVersion).products());
    }

    @GetMapping("/retrieval")
    public ApiResponse<ProductSearchResult> retrieval(
            @RequestParam String query,
            @RequestParam(required = false) String category,
            @RequestParam(required = false) String brand,
            @RequestParam(required = false) Long minPriceMinor,
            @RequestParam(required = false) Long maxPriceMinor,
            @RequestParam(required = false) Integer limit,
            @RequestParam(required = false) String catalogVersion
    ) {
        return ApiResponse.ok(service.searchWithTrace(
                query, category, brand, minPriceMinor, maxPriceMinor, limit,catalogVersion));
    }

    @GetMapping("/{id}")
    public ResponseEntity<ApiResponse<ProductDetailResponse>> get(@PathVariable Long id) {
        return service.get(id)
                .map(value -> ResponseEntity.ok(ApiResponse.ok(value)))
                .orElseGet(() -> ResponseEntity.status(HttpStatus.NOT_FOUND)
                        .body(ApiResponse.fail("商品不存在")));
    }

    @PostMapping("/resolve")
    public ApiResponse<List<ProductDetailResponse>> resolve(@Valid @RequestBody ResolveProductsRequest request) {
        return ApiResponse.ok(service.resolve(request.productIds()));
    }
}
