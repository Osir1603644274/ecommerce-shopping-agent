package com.example.locallife.flashsale;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.PositiveOrZero;

import java.time.OffsetDateTime;

public record CreateFlashSaleCampaignRequest(
        @NotNull
        @Pattern(regexp = "PRODUCT|LOCAL_DEAL") String itemType,
        @NotNull @Positive Long itemId,
        @NotBlank String title,
        @NotNull @PositiveOrZero Long salePriceMinor,
        @NotNull @Positive Integer totalStock,
        @NotNull OffsetDateTime startsAt,
        @NotNull OffsetDateTime endsAt
) {
}
