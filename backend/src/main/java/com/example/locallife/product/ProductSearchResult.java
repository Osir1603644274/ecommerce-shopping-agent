package com.example.locallife.product;

import java.util.List;

/** Search candidates plus an explicit record of the active/fallback recall channel. */
public record ProductSearchResult(
        List<ProductSummaryResponse> products,
        String channel,
        List<String> degradedChannels,
        int recallCount,
        int authoritativeEligibleCount,
        String factAuthority
) {
    public ProductSearchResult(
            List<ProductSummaryResponse> products,
            String channel,
            List<String> degradedChannels
    ) {
        this(products, channel, degradedChannels, products.size(), products.size(), "mysql");
    }
}
