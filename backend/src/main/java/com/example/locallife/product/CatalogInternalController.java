package com.example.locallife.product;

import com.example.locallife.common.ApiResponse;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Optional;

/** Internal catalog endpoints protected by an internal token.
 *
 *  These are called by the Python CatalogIndexManager to discover new catalog
 *  versions and page through product IDs for offline BM25 index construction.
 *  They are NOT intended for the public Agent-facing /api/products surface.
 */
@RestController
@RequestMapping("/internal/catalog")
public class CatalogInternalController {
    private final CatalogVersionRepository repository;

    public CatalogInternalController(CatalogVersionRepository repository) {
        this.repository = repository;
    }

    @GetMapping("/manifest")
    public ApiResponse<CatalogManifestResponse> manifest() {
        Optional<CatalogManifestResponse> latest = repository.getLatestManifest();
        return latest.map(ApiResponse::ok)
                .orElseGet(() -> ApiResponse.fail("no catalog version published"));
    }

    @GetMapping("/products")
    public ApiResponse<CatalogPageResponse> products(
            @RequestParam(required = false) String catalogVersion,
            @RequestParam(required = false) Long afterId,
            @RequestParam(required = false, defaultValue = "200") int limit
    ) {
        String version = catalogVersion != null ? catalogVersion : "";
        if (version.isBlank()) {
            return ApiResponse.fail("catalogVersion is required");
        }
        int cappedLimit = Math.min(Math.max(limit, 10), 500);
        CatalogPageResponse page = repository.getProducts(version, afterId, cappedLimit);
        if (!page.complete() && page.productCount() == 0) {
            return ApiResponse.fail("version mismatch — rebuild required");
        }
        return ApiResponse.ok(page);
    }
}
