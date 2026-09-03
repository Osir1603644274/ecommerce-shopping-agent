package com.example.locallife.product;

import jakarta.validation.constraints.AssertTrue;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;

public record CreateProductRequest(
        @NotNull @Positive Long id,
        @NotNull @Size(max = 64) @Pattern(regexp = ".*\\S.*") String source,
        @NotNull @Size(max = 128) @Pattern(regexp = ".*\\S.*") String sourceItemId,
        @NotNull @Size(max = 512) @Pattern(regexp = ".*\\S.*") String title,
        @NotNull @Size(max = 128) @Pattern(regexp = ".*\\S.*") String brand,
        @NotNull @Size(max = 255) @Pattern(regexp = ".*\\S.*") String seller,
        @NotNull @Size(max = 128) @Pattern(regexp = ".*\\S.*") String categoryL1,
        @NotNull @Size(max = 128) @Pattern(regexp = ".*\\S.*") String categoryL2,
        @NotNull @Size(max = 128) @Pattern(regexp = ".*\\S.*") String categoryL3,
        @PositiveOrZero Long snapshotPriceMinor,
        @Pattern(regexp = "[A-Z]{3}") String currency,
        @NotNull @Pattern(regexp = "verified|unverified|missing") String priceStatus,
        @Size(max = 8000) String attributeText,
        @NotNull @Size(max = 64) @Pattern(regexp = ".*\\S.*") String dataNature,
        @NotNull @Size(max = 128) @Pattern(regexp = ".*\\S.*") String datasetRevision,
        @NotNull @Size(max = 64) @Pattern(regexp = ".*\\S.*") String sourceLicense,
        @NotNull @Size(max = 512) @Pattern(regexp = ".*\\S.*") String provenanceUrl
) {
    public CreateProductRequest {
        source = strip(source);
        sourceItemId = strip(sourceItemId);
        title = strip(title);
        brand = strip(brand);
        seller = strip(seller);
        categoryL1 = strip(categoryL1);
        categoryL2 = strip(categoryL2);
        categoryL3 = strip(categoryL3);
        currency = strip(currency);
        priceStatus = strip(priceStatus);
        attributeText = strip(attributeText);
        dataNature = strip(dataNature);
        datasetRevision = strip(datasetRevision);
        sourceLicense = strip(sourceLicense);
        provenanceUrl = strip(provenanceUrl);
    }

    @AssertTrue(message = "已核验价格必须同时包含金额和币种")
    public boolean isPriceStateValid() {
        return !"verified".equals(priceStatus)
                || (snapshotPriceMinor != null && currency != null);
    }

    private static String strip(String value) {
        return value == null ? null : value.strip();
    }
}
