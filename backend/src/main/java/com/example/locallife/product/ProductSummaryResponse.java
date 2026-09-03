package com.example.locallife.product;

import java.time.LocalDateTime;

public record ProductSummaryResponse(
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
        String attributeText,
        String dataNature,
        String datasetRevision,
        String sourceLicense,
        String provenanceUrl,
        LocalDateTime importedAt
) {
}
