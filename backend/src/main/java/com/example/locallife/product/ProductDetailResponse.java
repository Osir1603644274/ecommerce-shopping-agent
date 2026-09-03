package com.example.locallife.product;

import java.time.LocalDateTime;
import java.util.List;

public record ProductDetailResponse(
        Long id,
        String source,
        String sourceItemId,
        String title,
        String brand,
        String seller,
        String categoryL1,
        String categoryL2,
        String categoryL3,
        Long snapshotPriceMinor,
        String currency,
        String priceStatus,
        String lifecycleStatus,
        Long entityVersion,
        Integer availableQuantity,
        Long inventoryVersion,
        String attributeText,
        String dataNature,
        String datasetRevision,
        String sourceLicense,
        String provenanceUrl,
        LocalDateTime importedAt,
        List<ProductAttributeResponse> attributes
) {
}
